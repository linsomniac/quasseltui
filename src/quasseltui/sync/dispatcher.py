"""Route inbound SignalProxy messages to SyncObjects and emit client events.

The dispatcher is the hinge between the protocol layer and the client-facing
view. It owns three responsibilities:

1. A `(class_name, object_name) -> SyncObject` registry. Every `Sync` or
   `InitData` frame the core sends identifies an object by that tuple; we
   look it up, create it on demand if we recognize the class, or log-and-
   drop if we don't.
2. A per-class factory map from `class_name` to the concrete `SyncObject`
   subclass. Register once in `__init__`; the dispatcher never mutates the
   factory map at runtime.
3. An `emit` callback into which it pushes `ClientEvent`s as side effects
   of dispatch. The callback is synchronous and must not block — the
   dispatcher is driven from inside `QuasselClient.events()` and back-
   pressure would stall the protocol read loop.

The dispatcher also mutates `ClientState` directly for things like buffer
metadata that have no dedicated SyncObject (buffers live in
`state.buffers` keyed by `BufferId`). The state object is the single source
of truth for the UI; the dispatcher is the single writer to it.

Design note on `displayMsg`: live IRC messages arrive as a top-level
`RpcCall(signalName="2displayMsg(Message)", params=[Message])` — not as a
`Sync` on any particular object. We intercept the signal name here and
emit `MessageReceived`. Anything else the core throws at us via `RpcCall`
(a `2connectToNetwork(NetworkId)` loopback, for example) is ignored for
phase 5 — those are write-side slots we drive, not receive.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from quasseltui.protocol.messages import SessionInit
from quasseltui.protocol.signalproxy import InitData, RpcCall, SyncMessage
from quasseltui.protocol.usertypes import BufferId, BufferInfo, IdentityId, Message, NetworkId
from quasseltui.sync.backlog_manager import BacklogManager
from quasseltui.sync.base import SyncObject
from quasseltui.sync.buffer_syncer import BufferSyncer
from quasseltui.sync.events import (
    BacklogReceived,
    BufferAdded,
    BufferRemoved,
    BufferRenamed,
    ClientEvent,
    IdentityAdded,
    IrcMessage,
    MessageReceived,
    NetworkAdded,
    NetworkUpdated,
    SessionOpened,
)
from quasseltui.sync.identity import Identity
from quasseltui.sync.irc_channel import IrcChannel
from quasseltui.sync.irc_user import IrcUser
from quasseltui.sync.network import Network

_log = logging.getLogger(__name__)

# The RpcCall signal name the core uses to announce a new IRC message.
# The `"2"` prefix is Qt's `QSIGNAL` macro — every signal name starts with
# `2` and every slot with `1` in SignalProxy wire format. We strip the
# prefix at comparison time so callers see the bare signature.
DISPLAY_MSG_SIGNAL = b"2displayMsg(Message)"


# Slot names whose success should turn into a `NetworkUpdated` event. Value
# is the `Network` attribute name to read after the mutation; the same name
# is reported as `NetworkUpdated.field_name` so a UI handler can switch on a
# stable tag. Deliberately narrow — we don't want to drown the UI in
# NetworkUpdated on every setLatency call.
_NETWORK_UPDATE_SLOTS: dict[bytes, str] = {
    b"setNetworkName": "network_name",
    b"setCurrentServer": "current_server",
    b"setMyNick": "my_nick",
    b"setConnectionState": "connection_state",
    b"setConnected": "is_connected",
}


class Dispatcher:
    """Routes inbound SignalProxy frames and mutates `ClientState`.

    `emit` is the callback the client uses to collect public events. It's
    passed in so the dispatcher itself doesn't need to know whether events
    go to a Textual message queue, an async iterator, or a test list.
    `state` is imported lazily via type-only import to avoid a cross-layer
    dependency; the import is safe because `client/state.py` re-exports
    what it needs from `sync/` rather than the other way around.

    The class-factory map is hard-wired for v1 — every SyncObject subclass
    we ship has a fixed role in the graph, so there's no value in letting
    callers register new ones at runtime.
    """

    def __init__(
        self,
        state: Any,  # client.state.ClientState - typed at use site to avoid import cycle
        emit: Callable[[ClientEvent], None],
    ) -> None:
        self._state = state
        self._emit = emit
        self._objects: dict[tuple[bytes, str], SyncObject] = {}
        self._factories: dict[bytes, type[SyncObject]] = {
            Network.CLASS_NAME: Network,
            IrcChannel.CLASS_NAME: IrcChannel,
            IrcUser.CLASS_NAME: IrcUser,
            Identity.CLASS_NAME: Identity,
            BufferSyncer.CLASS_NAME: BufferSyncer,
            BacklogManager.CLASS_NAME: BacklogManager,
        }

    # -- public introspection ------------------------------------------------

    @property
    def objects(self) -> dict[tuple[bytes, str], SyncObject]:
        """Read-only view for tests. Mutating this dict is not supported."""
        return self._objects

    def get(self, class_name: bytes, object_name: str) -> SyncObject | None:
        return self._objects.get((class_name, object_name))

    # -- session seeding -----------------------------------------------------

    def seed_from_session(
        self,
        session: SessionInit,
        peer_features: frozenset[str],
    ) -> None:
        """Populate `ClientState` from a fresh `SessionInit`.

        Called exactly once, immediately after the handshake finishes.
        Emits `SessionOpened` first, then `NetworkAdded` / `BufferAdded` /
        `IdentityAdded` for everything the core announced in the session.
        Actual network *state* (name, my_nick, ...) arrives later via
        `InitData` messages — the Network SyncObjects we create here start
        out empty and get filled in asynchronously.
        """
        self._state.session = session
        self._state.peer_features = peer_features
        self._emit(SessionOpened(session=session, peer_features=peer_features))

        # Create Network placeholders
        for nid in session.network_ids:
            obj_name = str(int(nid))
            network = Network(object_name=obj_name)
            self._register(network)
            self._state.networks[nid] = network
            self._emit(NetworkAdded(network_id=nid, name=""))

        # Register buffers (no SyncObject — buffers are records, not syncables)
        for buf in session.buffer_infos:
            self._state.buffers[buf.buffer_id] = buf
            self._state.messages.setdefault(buf.buffer_id, [])
            self._emit(
                BufferAdded(
                    buffer_id=buf.buffer_id,
                    network_id=buf.network_id,
                    name=buf.name,
                    type=buf.type,
                )
            )

        # Seed identities from the raw session identities list
        for raw_ident in session.identities:
            ident_id_raw = raw_ident.get("identityId") or raw_ident.get("IdentityId")
            if not isinstance(ident_id_raw, int):
                continue
            ident_id = IdentityId(int(ident_id_raw))
            identity = Identity(object_name=str(int(ident_id)))
            identity.apply_init_data(raw_ident)
            self._register(identity)
            self._state.identities[ident_id] = identity
            self._emit(IdentityAdded(identity_id=ident_id, name=identity.identity_name))

        # BufferSyncer singleton
        buffer_syncer = BufferSyncer(object_name="")
        self._register(buffer_syncer)
        self._state.buffer_syncer = buffer_syncer

        # BacklogManager singleton — receives backlog responses
        backlog_mgr = BacklogManager(object_name="")
        self._register(backlog_mgr)

    # -- Sync dispatch -------------------------------------------------------

    def handle_sync(self, msg: SyncMessage) -> None:
        """Route a `Sync` call to the right SyncObject and emit any events."""
        obj = self._lookup_or_create(msg.class_name, msg.object_name)
        if obj is None:
            _log.debug("ignoring Sync for unknown class %r::%r", msg.class_name, msg.object_name)
            return
        obj.handle_sync(msg.slot_name, list(msg.params))
        self._emit_slot_side_effects(msg.class_name, msg.object_name, msg.slot_name, obj)

    # -- InitData dispatch ---------------------------------------------------

    def handle_init_data(self, msg: InitData) -> None:
        """Apply an `InitData` property map to the matching SyncObject.

        If the object doesn't exist yet (the core sent InitData before we
        Sync'd anything on it), we create it from the factory map. After the
        object has been populated we do the cross-object expansion for a few
        special cases:

        - `Network.IrcUsersAndChannels`: creates `IrcUser` / `IrcChannel`
          instances for every entry in the nested seed maps. The dispatcher
          knows the object-name conventions (`"<netId>/<nick>"` etc.) and
          the Network doesn't, so it has to live here.
        """
        obj = self._lookup_or_create(msg.class_name, msg.object_name)
        if obj is None:
            _log.debug(
                "ignoring InitData for unknown class %r::%r",
                msg.class_name,
                msg.object_name,
            )
            return
        obj.apply_init_data(msg.init_data)

        if isinstance(obj, Network):
            self._expand_network_init(obj)
            # The name was probably unknown at session time; refresh it now.
            self._emit(
                NetworkUpdated(
                    network_id=NetworkId(obj.network_id),
                    field_name="network_name",
                    value=obj.network_name,
                )
            )
        elif isinstance(obj, Identity):
            # An identity can be re-initialized (e.g. the user edited it).
            # Re-emit NamedAdded so a UI that missed the first one still
            # sees it — caller can dedupe by identity_id if it cares.
            self._emit(
                IdentityAdded(
                    identity_id=IdentityId(obj.identity_id),
                    name=obj.identity_name,
                )
            )

    # -- RpcCall dispatch ----------------------------------------------------

    def handle_rpc(self, msg: RpcCall) -> None:
        """Handle top-level `RpcCall`s that aren't routed to a SyncObject.

        Today we only care about `displayMsg(Message)`. Everything else is
        silently dropped — the core does send occasional other RPC signals
        (`connectToNetwork`, etc.) that are client-to-core directional and
        have no meaning when the core sends them back to us.
        """
        if msg.signal_name != DISPLAY_MSG_SIGNAL:
            _log.debug("ignoring RpcCall %r with %d params", msg.signal_name, len(msg.params))
            return
        if not msg.params:
            _log.warning("displayMsg with no payload")
            return
        raw = msg.params[0]
        if not isinstance(raw, Message):
            _log.warning("displayMsg expected Message, got %s", type(raw).__name__)
            return
        self._store_and_emit_message(raw)

    # -- internals -----------------------------------------------------------

    def _register(self, obj: SyncObject) -> None:
        key = (type(obj).CLASS_NAME, obj.object_name)
        self._objects[key] = obj

    def _lookup_or_create(self, class_name: bytes, object_name: str) -> SyncObject | None:
        """Return an existing SyncObject or create one from the factory map."""
        key = (class_name, object_name)
        obj = self._objects.get(key)
        if obj is not None:
            return obj
        factory = self._factories.get(class_name)
        if factory is None:
            return None
        obj = factory(object_name)
        self._register(obj)
        self._link_new_object(obj)
        return obj

    def _link_new_object(self, obj: SyncObject) -> None:
        """Hook newly-created SyncObjects into `ClientState` collections."""
        if isinstance(obj, Network):
            nid = NetworkId(obj.network_id)
            if nid not in self._state.networks:
                self._state.networks[nid] = obj
                self._emit(NetworkAdded(network_id=nid, name=obj.network_name))
        elif isinstance(obj, Identity):
            ident_id = IdentityId(obj.identity_id)
            if ident_id not in self._state.identities:
                self._state.identities[ident_id] = obj

    def _expand_network_init(self, network: Network) -> None:
        """After a Network's InitData, materialize its Users and Channels.

        Quassel ships the entire roster for a network as a single nested
        `IrcUsersAndChannels` QVariantMap in the Network's init data.
        Expanding it here keeps the object-name convention knowledge
        (`"<netId>/<nick>"`, `"<netId>/<channel>"`) centralized — Network
        itself shouldn't need to know how IrcUser/IrcChannel identify
        themselves.
        """
        net_id = network.network_id
        for nick, raw_fields in network.users_seed.items():
            obj_name = f"{net_id}/{nick}"
            key = (IrcUser.CLASS_NAME, obj_name)
            existing = self._objects.get(key)
            if existing is None:
                user = IrcUser(object_name=obj_name)
                user.apply_init_data(raw_fields)
                self._register(user)
            else:
                # Re-seed — the user object may have been created via a
                # Sync call before we saw the Network InitData.
                existing.apply_init_data(raw_fields)
        for chan_name, raw_fields in network.channels_seed.items():
            obj_name = f"{net_id}/{chan_name}"
            key = (IrcChannel.CLASS_NAME, obj_name)
            existing = self._objects.get(key)
            if existing is None:
                channel = IrcChannel(object_name=obj_name)
                channel.apply_init_data(raw_fields)
                self._register(channel)
            else:
                existing.apply_init_data(raw_fields)

    def _emit_slot_side_effects(
        self,
        class_name: bytes,
        object_name: str,
        slot_name: bytes,
        obj: SyncObject,
    ) -> None:
        """Turn a just-completed Sync call into any applicable public event.

        The BufferSyncer branch drains *all* of its pending-change sets on
        every slot call (removals, merges, renames). That's deliberate:
        the slot handlers accumulate into the sets and are cheap, the
        drain is O(pending) which is usually zero, and this pattern
        guarantees that a rename followed by a remove emits BOTH events
        in the right order regardless of which slot happened to trigger
        the drain.
        """
        if class_name == Network.CLASS_NAME and slot_name in _NETWORK_UPDATE_SLOTS:
            field_name = _NETWORK_UPDATE_SLOTS[slot_name]
            assert isinstance(obj, Network)
            new_value = getattr(obj, field_name, None)
            self._emit(
                NetworkUpdated(
                    network_id=NetworkId(obj.network_id),
                    field_name=field_name,
                    value=new_value,
                )
            )
            return
        if class_name == BufferSyncer.CLASS_NAME:
            assert isinstance(obj, BufferSyncer)
            self._drain_buffer_syncer_pending(obj)
            return
        if class_name == BacklogManager.CLASS_NAME and slot_name == b"receiveBacklog":
            assert isinstance(obj, BacklogManager)
            self._merge_backlog(obj)

    def _drain_buffer_syncer_pending(self, syncer: BufferSyncer) -> None:
        """Emit BufferRemoved / BufferRenamed for pending BufferSyncer ops.

        Called after every BufferSyncer slot call. Splitting this into its
        own method keeps the side-effects method short and makes the
        "always drains pending, never interleaves rename inside remove"
        contract visible.
        """
        if syncer.removed_buffers:
            for bid in list(syncer.removed_buffers):
                buffer_id = BufferId(bid)
                # Only emit if we actually had the buffer in state — a
                # second removeBuffer for an ID we already dropped is a
                # no-op rather than a double-emit. This matches what the
                # core does when it broadcasts the same removal to every
                # client and one of them has already processed it.
                if buffer_id in self._state.buffers:
                    del self._state.buffers[buffer_id]
                    self._state.messages.pop(buffer_id, None)
                    self._emit(BufferRemoved(buffer_id=buffer_id))
            syncer.removed_buffers.clear()
        if syncer.renamed_buffers:
            for bid, new_name in list(syncer.renamed_buffers.items()):
                buffer_id = BufferId(bid)
                existing = self._state.buffers.get(buffer_id)
                if existing is not None:
                    # BufferInfo is a frozen dataclass — we have to rebuild
                    # it rather than mutate. Only `name` changes; the other
                    # fields are carried across verbatim.
                    renamed = BufferInfo(
                        buffer_id=existing.buffer_id,
                        network_id=existing.network_id,
                        type=existing.type,
                        group_id=existing.group_id,
                        name=new_name,
                    )
                    self._state.buffers[buffer_id] = renamed
                self._emit(BufferRenamed(buffer_id=buffer_id, name=new_name))
            syncer.renamed_buffers.clear()

    def _merge_backlog(self, mgr: BacklogManager) -> None:
        """Convert raw backlog Messages and prepend to state, deduped.

        Called after the BacklogManager's `receiveBacklog` slot has
        stashed the raw Messages on `mgr.last_received`. We convert
        each to `IrcMessage`, deduplicate by `msg_id` against the
        existing list *and* within the batch, sort by `msg_id`, and
        emit `BacklogReceived`.

        Uses the slot's authoritative `buffer_id` rather than trusting
        payload contents. Messages whose `buffer_info.buffer_id`
        doesn't match the slot's are dropped. If the target buffer has
        been removed (no longer in `state.buffers`), the entire reply
        is silently discarded — a late backlog reply must not resurrect
        a buffer that the core already told us to remove.
        """
        raw_messages = mgr.last_received
        mgr.last_received = []
        # AIDEV-NOTE: buffer_id comes from the slot param, not from
        # the payload messages — prevents a hostile/buggy core from
        # corrupting per-buffer history by mixing buffer_ids.
        buffer_id = BufferId(int(mgr.last_buffer_id)) if mgr.last_buffer_id is not None else None
        mgr.last_buffer_id = None
        if not raw_messages or buffer_id is None:
            return
        if buffer_id not in self._state.buffers:
            _log.debug("dropping backlog for removed buffer %d", int(buffer_id))
            return
        existing = self._state.messages.setdefault(buffer_id, [])
        seen_ids = {m.msg_id for m in existing}
        new_messages: list[IrcMessage] = []
        for raw in raw_messages:
            if raw.buffer_info.buffer_id != buffer_id:
                continue
            if raw.msg_id in seen_ids:
                continue
            seen_ids.add(raw.msg_id)
            narrow = IrcMessage(
                msg_id=raw.msg_id,
                buffer_id=buffer_id,
                network_id=raw.buffer_info.network_id,
                timestamp=raw.timestamp,
                type=raw.type,
                flags=raw.flags,
                sender=raw.sender,
                sender_prefixes=raw.sender_prefixes,
                contents=raw.contents,
            )
            new_messages.append(narrow)
        if new_messages:
            existing.extend(new_messages)
            existing.sort(key=lambda m: int(m.msg_id))
        cap = self._state.max_messages_per_buffer
        if cap > 0 and len(existing) > cap:
            del existing[: len(existing) - cap]
        self._emit(BacklogReceived(buffer_id=buffer_id, count=len(new_messages)))

    def _store_and_emit_message(self, raw: Message) -> None:
        """Append a decoded `Message` to `state.messages` and emit the event.

        Enforces `state.max_messages_per_buffer` as a hard retention cap:
        once a buffer's message list exceeds the cap, the oldest messages
        are dropped to bring it back down. Without this, a noisy channel
        (or a malicious peer) could inflate memory unbounded over a
        long-lived session. A cap of 0 disables retention (which means
        the list grows forever — use only for offline tests).
        """
        buffer_info: BufferInfo = raw.buffer_info
        buffer_id = buffer_info.buffer_id
        self._state.buffers.setdefault(buffer_id, buffer_info)
        message_list = self._state.messages.setdefault(buffer_id, [])
        narrow = IrcMessage(
            msg_id=raw.msg_id,
            buffer_id=buffer_id,
            network_id=buffer_info.network_id,
            timestamp=raw.timestamp,
            type=raw.type,
            flags=raw.flags,
            sender=raw.sender,
            sender_prefixes=raw.sender_prefixes,
            contents=raw.contents,
        )
        message_list.append(narrow)
        cap = self._state.max_messages_per_buffer
        if cap > 0 and len(message_list) > cap:
            # Drop the oldest N so we land exactly at the cap. `del` on a
            # slice is O(n) but only runs when the list is already oversize,
            # and n is the overshoot (usually 1 on a steady stream).
            del message_list[: len(message_list) - cap]
        self._emit(MessageReceived(message=narrow))


__all__ = [
    "DISPLAY_MSG_SIGNAL",
    "Dispatcher",
]
