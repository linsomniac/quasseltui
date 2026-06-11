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
from quasseltui.protocol.usertypes import (
    BufferId,
    BufferInfo,
    IdentityId,
    Message,
    MsgId,
    NetworkId,
)
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
    NetworkRemoved,
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

# SignalProxy's object-rename broadcast (no "2" prefix — it's an internal
# SignalProxy control message, not a Qt signal). Params:
# [className: bytes, newName: str, oldName: str]. Quassel re-addresses
# IrcUser syncables this way on every nick change.
OBJECT_RENAMED_SIGNAL = b"__objectRenamed__"

# Network lifecycle broadcasts — sent when a network is added/removed on
# the core (e.g. from a desktop client connected to the same core).
NETWORK_CREATED_SIGNAL = b"2networkCreated(NetworkId)"
NETWORK_REMOVED_SIGNAL = b"2networkRemoved(NetworkId)"


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


def _as_network_id(value: Any) -> NetworkId | None:
    """Coerce a wire param (NetworkId user type or plain int) to NetworkId."""
    if isinstance(value, bool):
        return None
    if isinstance(value, NetworkId):
        return value
    try:
        return NetworkId(int(value))
    except (TypeError, ValueError):
        return None


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

        Called exactly once per dispatcher, immediately after the
        handshake finishes. Emits `SessionOpened` first, then
        `NetworkAdded` / `BufferAdded` / `IdentityAdded` for everything
        the core announced in the session. Actual network *state*
        (name, my_nick, ...) arrives later via `InitData` messages —
        the Network SyncObjects we create here start out empty and get
        filled in asynchronously.

        Reconnect note: the state may be a REUSED `ClientState` from a
        previous session (so message history survives a reconnect).
        Per-session latches are reset here: clearing
        `backlog_requested` makes the next buffer switch re-request
        history, and the msg_id dedup in `_merge_backlog` makes that
        re-request safely fill the gap since the disconnect.
        """
        self._state.session = session
        self._state.peer_features = peer_features
        self._state.backlog_requested.clear()
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

        # Seed identities from the raw session identities list. Real
        # cores wrap identityId in the IdentityId user type (decoded to
        # the IdentityId dataclass, not a plain int) — accepting only
        # int silently dropped every identity from a real core.
        for raw_ident in session.identities:
            ident_id_raw = raw_ident.get("identityId") or raw_ident.get("IdentityId")
            if isinstance(ident_id_raw, bool) or not isinstance(ident_id_raw, int | IdentityId):
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
        self._emit_slot_side_effects(msg.class_name, msg.slot_name, obj, list(msg.params))

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
        elif isinstance(obj, BufferSyncer):
            self._seed_read_markers(obj)

    def _seed_read_markers(self, syncer: BufferSyncer) -> None:
        """Adopt the core's persisted marker lines as local read markers.

        The core stores marker lines across sessions; without this seed a
        marker placed in a previous run (or from another client) never
        shows up here. `setdefault` so a marker the user already placed
        in THIS session isn't clobbered by late-arriving InitData. The
        core uses -1 for "no marker".
        """
        for bid, mid in syncer.marker_lines_by_buffer.items():
            if mid < 0:
                continue
            self._state.read_markers.setdefault(BufferId(bid), MsgId(mid))

    # -- RpcCall dispatch ----------------------------------------------------

    def handle_rpc(self, msg: RpcCall) -> None:
        """Handle top-level `RpcCall`s that aren't routed to a SyncObject.

        Recognized signals: `displayMsg(Message)` (live IRC traffic),
        `__objectRenamed__` (nick changes re-address IrcUser syncables),
        and `networkCreated`/`networkRemoved` (network lifecycle from
        other clients). Everything else is silently dropped — the core
        does send occasional other RPC signals (`connectToNetwork`, etc.)
        that are client-to-core directional and have no meaning when the
        core sends them back to us.
        """
        if msg.signal_name == DISPLAY_MSG_SIGNAL:
            if not msg.params:
                _log.warning("displayMsg with no payload")
                return
            raw = msg.params[0]
            if not isinstance(raw, Message):
                _log.warning("displayMsg expected Message, got %s", type(raw).__name__)
                return
            self._store_and_emit_message(raw)
            return
        if msg.signal_name == OBJECT_RENAMED_SIGNAL:
            self._handle_object_renamed(list(msg.params))
            return
        if msg.signal_name == NETWORK_CREATED_SIGNAL:
            self._handle_network_created(list(msg.params))
            return
        if msg.signal_name == NETWORK_REMOVED_SIGNAL:
            self._handle_network_removed(list(msg.params))
            return
        _log.debug("ignoring RpcCall %r with %d params", msg.signal_name, len(msg.params))

    def _handle_object_renamed(self, params: list[Any]) -> None:
        """Re-key a SyncObject after a SignalProxy `__objectRenamed__`.

        Quassel re-addresses IrcUser objects on nick change (IrcUser::
        setNick -> updateObjectName -> renameObject) and addresses every
        subsequent Sync frame to the NEW name. Without the re-key, the
        object strands under its old key: later updates for the user
        either get silently dropped or create a duplicate empty object.
        """
        if len(params) < 3:
            _log.warning("__objectRenamed__ with %d params (expected 3)", len(params))
            return
        raw_class, new_raw, old_raw = params[0], params[1], params[2]
        class_name = raw_class.encode() if isinstance(raw_class, str) else raw_class
        if not isinstance(class_name, bytes) or new_raw is None or old_raw is None:
            _log.warning("__objectRenamed__ with malformed params: %r", params)
            return
        new_name, old_name = str(new_raw), str(old_raw)
        obj = self._objects.pop((class_name, old_name), None)
        if obj is None:
            _log.debug("__objectRenamed__ for unknown %r::%r", class_name, old_name)
            return
        if isinstance(obj, IrcUser):
            old_nick = obj.nick
            obj.rename(new_name)
            self._objects[(class_name, new_name)] = obj
            self._rekey_user_rosters(obj, old_nick)
        else:
            obj.object_name = new_name
            self._objects[(class_name, new_name)] = obj

    def _rekey_user_rosters(self, user: IrcUser, old_nick: str) -> None:
        """Move a renamed user's roster entries to the new nick."""
        new_nick = user.nick
        if old_nick == new_nick:
            return
        network = self._state.networks.get(NetworkId(user.network_id))
        if network is not None and old_nick in network.users:
            network.users.discard(old_nick)
            network.users.add(new_nick)
        for obj in self._objects.values():
            if (
                isinstance(obj, IrcChannel)
                and obj.network_id == user.network_id
                and old_nick in obj.user_modes
            ):
                obj.user_modes[new_nick] = obj.user_modes.pop(old_nick)

    def _handle_network_created(self, params: list[Any]) -> None:
        nid = _as_network_id(params[0]) if params else None
        if nid is None:
            _log.warning("networkCreated with malformed params: %r", params)
            return
        if nid in self._state.networks:
            return
        network = Network(object_name=str(int(nid)))
        self._register(network)
        self._state.networks[nid] = network
        # Name/state arrive via the InitData the client requests on
        # seeing this event (QuasselClient._maybe_request_network_init).
        self._emit(NetworkAdded(network_id=nid, name=""))

    def _handle_network_removed(self, params: list[Any]) -> None:
        """Drop a removed network, its syncables, and all its buffers.

        Without this, a network deleted from another client persists in
        the sidebar for the rest of the session with no way to clear it.
        """
        nid = _as_network_id(params[0]) if params else None
        if nid is None:
            _log.warning("networkRemoved with malformed params: %r", params)
            return
        network = self._state.networks.pop(nid, None)
        if network is None:
            return
        self._objects.pop((Network.CLASS_NAME, str(int(nid))), None)
        stale = [
            key
            for key, obj in self._objects.items()
            if isinstance(obj, IrcUser | IrcChannel) and obj.network_id == int(nid)
        ]
        for key in stale:
            del self._objects[key]
        doomed = [bid for bid, info in self._state.buffers.items() if info.network_id == nid]
        for bid in doomed:
            del self._state.buffers[bid]
            self._state.messages.pop(bid, None)
            self._state.backlog_requested.discard(bid)
            self._state.read_markers.pop(bid, None)
            self._emit(BufferRemoved(buffer_id=bid))
        self._emit(NetworkRemoved(network_id=nid))

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
        slot_name: bytes,
        obj: SyncObject,
        params: list[Any],
    ) -> None:
        """Turn a just-completed Sync call into any applicable public event.

        The BufferSyncer branch drains *all* of its pending-change sets on
        every slot call (removals, merges, renames). That's deliberate:
        the slot handlers accumulate into the sets and are cheap, the
        drain is O(pending) which is usually zero, and this pattern
        guarantees that a rename followed by a remove emits BOTH events
        in the right order regardless of which slot happened to trigger
        the drain.

        The IrcUser branch cascades membership removal: real cores SYNC
        parts/kicks as IrcUser::partChannel and quits as IrcUser::quit
        (IrcChannel::part is NOT a sync method), so the channel rosters
        only stay correct if the dispatcher fans the removal out here.
        """
        if class_name == IrcUser.CLASS_NAME and isinstance(obj, IrcUser):
            if slot_name == b"partChannel" and params:
                self._cascade_user_part(obj, str(params[0]))
            elif slot_name == b"quit":
                self._cascade_user_quit(obj)
            return
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

    def _cascade_user_part(self, user: IrcUser, channel_name: str) -> None:
        chan = self._objects.get((IrcChannel.CLASS_NAME, f"{user.network_id}/{channel_name}"))
        if isinstance(chan, IrcChannel):
            chan.user_modes.pop(user.nick, None)

    def _cascade_user_quit(self, user: IrcUser) -> None:
        """Remove a quit user from every roster and from the registry.

        Iterates all channels of the network rather than `user.channels`
        because the quit slot has already cleared that set by the time
        this hook runs — and the channel-side rosters are authoritative
        anyway.
        """
        nick = user.nick
        for obj in self._objects.values():
            if isinstance(obj, IrcChannel) and obj.network_id == user.network_id:
                obj.user_modes.pop(nick, None)
        network = self._state.networks.get(NetworkId(user.network_id))
        if network is not None:
            network.users.discard(nick)
        self._objects.pop((IrcUser.CLASS_NAME, user.object_name), None)

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
        is discarded — a late backlog reply must not resurrect a buffer
        that the core already told us to remove — and the buffer's
        `backlog_requested` latch is cleared with it.

        Contract: every reply that maps to a live buffer emits exactly
        one `BacklogReceived`, including empty replies (count=0). The
        UI relies on the event to know the request completed; a silent
        empty reply is indistinguishable from the reply never arriving.
        """
        raw_messages = mgr.last_received
        mgr.last_received = []
        # AIDEV-NOTE: buffer_id comes from the slot param, not from
        # the payload messages — prevents a hostile/buggy core from
        # corrupting per-buffer history by mixing buffer_ids.
        buffer_id = BufferId(int(mgr.last_buffer_id)) if mgr.last_buffer_id is not None else None
        mgr.last_buffer_id = None
        if buffer_id is None:
            return
        if buffer_id not in self._state.buffers:
            # Late reply for a removed buffer: drop it, and unlatch the
            # request flag so a re-created buffer can request again.
            self._state.backlog_requested.discard(buffer_id)
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
        # AIDEV-NOTE: a buffer created mid-session (incoming PM, /join
        # result) has no dedicated wire signal — its first displayMsg
        # carries the new BufferInfo. Emit BufferAdded BEFORE
        # MessageReceived so the UI creates the tree node before it
        # routes the message; without this, mid-session buffers (and
        # every message in them) were invisible until restart.
        is_new_buffer = buffer_id not in self._state.buffers
        self._state.buffers.setdefault(buffer_id, buffer_info)
        if is_new_buffer:
            self._emit(
                BufferAdded(
                    buffer_id=buffer_id,
                    network_id=buffer_info.network_id,
                    name=buffer_info.name,
                    type=buffer_info.type,
                )
            )
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
