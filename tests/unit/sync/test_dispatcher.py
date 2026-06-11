"""Unit tests for `quasseltui.sync.dispatcher.Dispatcher`.

The dispatcher sits at the L2/L3 seam and is the single place that
combines the protocol-layer SignalProxy types, the sync-layer SyncObject
registry, and the client-layer `ClientState`. A bug here ripples
everywhere, so these tests are a bit heavier than the per-class ones:

- A whole `SessionInit` seed, including network ids and buffer infos,
  produces the expected `SessionOpened` + `NetworkAdded` + `BufferAdded`
  event sequence in order.
- A `SyncMessage(Network, ..., setNetworkName, ["freenode"])` mutates the
  network and emits a `NetworkUpdated(field_name="name", ...)`.
- An `InitData(Network, ..., {networkName, IrcUsersAndChannels: {...}})`
  materializes the nested `IrcUser` / `IrcChannel` children by constructing
  their C++ object-name strings (`"<netId>/<nick>"`).
- An `RpcCall(displayMsg, [Message])` lands in `state.messages` and emits
  a `MessageReceived`.
- `BufferSyncer.removeBuffer` emits `BufferRemoved` and drops the buffer
  from state.
"""

from __future__ import annotations

import datetime as dt

from quasseltui.client.state import ClientState
from quasseltui.protocol.enums import MessageFlag, MessageType
from quasseltui.protocol.messages import SessionInit
from quasseltui.protocol.signalproxy import InitData, RpcCall, SyncMessage
from quasseltui.protocol.usertypes import (
    BufferId,
    BufferInfo,
    BufferType,
    IdentityId,
    Message,
    MsgId,
    NetworkId,
)
from quasseltui.sync.buffer_syncer import BufferSyncer
from quasseltui.sync.dispatcher import DISPLAY_MSG_SIGNAL, Dispatcher
from quasseltui.sync.events import (
    BacklogReceived,
    BufferAdded,
    BufferRemoved,
    BufferRenamed,
    ClientEvent,
    IdentityAdded,
    MessageReceived,
    NetworkAdded,
    NetworkRemoved,
    NetworkUpdated,
    SessionOpened,
)
from quasseltui.sync.irc_channel import IrcChannel
from quasseltui.sync.irc_user import IrcUser


def _make_state_and_dispatcher() -> tuple[ClientState, Dispatcher, list[ClientEvent]]:
    state = ClientState()
    events: list[ClientEvent] = []
    dispatcher = Dispatcher(state=state, emit=events.append)
    return state, dispatcher, events


_DEFAULT_IDENTITIES: tuple[dict[str, object], ...] = ({"identityId": 1, "identityName": "default"},)


def _session(
    *,
    network_ids: list[int] | None = None,
    identities: list[dict[str, object]] | None = None,
    buffer_infos: list[BufferInfo] | None = None,
) -> SessionInit:
    net_ids = [1] if network_ids is None else network_ids
    ids = list(_DEFAULT_IDENTITIES) if identities is None else identities
    bufs: list[BufferInfo] = [] if buffer_infos is None else buffer_infos
    return SessionInit(
        identities=tuple(ids),
        network_ids=tuple(NetworkId(i) for i in net_ids),
        buffer_infos=tuple(bufs),
        raw={"SessionState": {}},
    )


def _buffer(buffer_id: int, network_id: int, name: str) -> BufferInfo:
    return BufferInfo(
        buffer_id=BufferId(buffer_id),
        network_id=NetworkId(network_id),
        type=BufferType.Channel,
        group_id=0,
        name=name,
    )


class TestSeedFromSession:
    def test_empty_session_still_emits_session_opened(self) -> None:
        state, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[], identities=[]), frozenset())
        assert events and isinstance(events[0], SessionOpened)
        assert state.buffer_syncer is not None
        assert state.buffer_syncer.object_name == ""

    def test_networks_and_identities_are_seeded(self) -> None:
        state, dispatcher, events = _make_state_and_dispatcher()
        session = _session(
            network_ids=[1, 5],
            identities=[
                {"identityId": 1, "identityName": "default"},
                {"identityId": 7, "identityName": "alt", "nicks": ["alt"]},
            ],
        )
        dispatcher.seed_from_session(session, frozenset({"LongTime"}))
        assert state.peer_features == frozenset({"LongTime"})
        assert NetworkId(1) in state.networks
        assert NetworkId(5) in state.networks
        assert state.networks[NetworkId(1)].object_name == "1"
        # Identity seeded via its raw dict (apply_init_data ran).
        assert state.identities[IdentityId(1)].identity_name == "default"
        assert state.identities[IdentityId(7)].nicks == ["alt"]
        # Ordering: SessionOpened first, NetworkAdded next, IdentityAdded after.
        kinds = [type(e).__name__ for e in events]
        assert kinds[0] == "SessionOpened"
        assert kinds[1] == "NetworkAdded"

    def test_buffers_seeded_into_state_and_emit_buffer_added(self) -> None:
        state, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(
            _session(
                network_ids=[1],
                buffer_infos=[_buffer(10, 1, "#python"), _buffer(11, 1, "#rust")],
            ),
            frozenset(),
        )
        assert state.buffers[BufferId(10)].name == "#python"
        assert state.buffers[BufferId(11)].name == "#rust"
        added = [e for e in events if isinstance(e, BufferAdded)]
        assert {a.name for a in added} == {"#python", "#rust"}
        # Every buffer has a pre-allocated empty message list so the UI
        # can mutate it without special-casing "first message".
        assert state.messages[BufferId(10)] == []


class TestHandleSync:
    def test_set_network_name_emits_network_updated(self) -> None:
        state, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[1]), frozenset())
        events.clear()

        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"Network",
                object_name="1",
                slot_name=b"setNetworkName",
                params=["freenode"],
            )
        )
        assert state.networks[NetworkId(1)].network_name == "freenode"
        updates = [e for e in events if isinstance(e, NetworkUpdated)]
        assert updates and updates[-1].field_name == "network_name"
        assert updates[-1].value == "freenode"

    def test_sync_on_unknown_class_is_dropped(self) -> None:
        _, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[]), frozenset())
        events.clear()
        # Unknown C++ class — the dispatcher has no factory for `ChatView`,
        # so it's logged and no event is emitted.
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"ChatView",
                object_name="whatever",
                slot_name=b"setSomething",
                params=[],
            )
        )
        assert events == []


class TestHandleInitData:
    def test_network_init_data_emits_network_updated_and_creates_children(self) -> None:
        state, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[3]), frozenset())
        events.clear()

        init = InitData(
            class_name=b"Network",
            object_name="3",
            init_data={
                "networkName": "rizon",
                "myNick": "seanr",
                "IrcUsersAndChannels": {
                    "Users": {
                        "seanr": {"nick": "seanr", "user": "sean", "host": "example.com"},
                    },
                    "Channels": {
                        "#python": {"name": "#python", "topic": "pythonistas"},
                    },
                },
            },
        )
        dispatcher.handle_init_data(init)

        net = state.networks[NetworkId(3)]
        assert net.network_name == "rizon"
        assert net.my_nick == "seanr"

        # The nested children were created with the dispatcher's object-name
        # convention: "<netId>/<nick>" for users, "<netId>/<name>" for
        # channels. Finding them via `get` is the contract test.
        user = dispatcher.get(IrcUser.CLASS_NAME, "3/seanr")
        assert isinstance(user, IrcUser)
        assert user.user == "sean"

        channel = dispatcher.get(IrcChannel.CLASS_NAME, "3/#python")
        assert isinstance(channel, IrcChannel)
        assert channel.topic == "pythonistas"

        # A NetworkUpdated event carrying the name is emitted so a UI can
        # refresh its label without re-reading the whole network.
        updates = [
            e for e in events if isinstance(e, NetworkUpdated) and e.field_name == "network_name"
        ]
        assert updates and updates[-1].value == "rizon"

    def test_identity_init_data_re_emits_identity_added(self) -> None:
        state, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(
            _session(
                network_ids=[],
                identities=[{"identityId": 42, "identityName": "default"}],
            ),
            frozenset(),
        )
        events.clear()

        init = InitData(
            class_name=b"Identity",
            object_name="42",
            init_data={"identityName": "renamed", "nicks": ["renamed"]},
        )
        dispatcher.handle_init_data(init)
        assert state.identities[IdentityId(42)].identity_name == "renamed"
        assert any(isinstance(e, IdentityAdded) for e in events)


class TestHandleRpc:
    def test_display_msg_emits_message_received(self) -> None:
        state, dispatcher, events = _make_state_and_dispatcher()
        buf = _buffer(10, 1, "#python")
        dispatcher.seed_from_session(
            _session(network_ids=[1], buffer_infos=[buf]), frozenset({"LongTime"})
        )
        events.clear()

        message = Message(
            msg_id=MsgId(123),
            timestamp=dt.datetime(2026, 4, 14, 12, 0, 0, tzinfo=dt.UTC),
            type=MessageType.Plain,
            flags=MessageFlag.NONE,
            buffer_info=buf,
            sender="sean!sean@example.com",
            sender_prefixes="@",
            real_name="",
            avatar_url="",
            contents="hello world",
            peer_features=frozenset({"LongTime"}),
        )
        dispatcher.handle_rpc(RpcCall(signal_name=DISPLAY_MSG_SIGNAL, params=[message]))
        assert len(state.messages[BufferId(10)]) == 1
        received = [e for e in events if isinstance(e, MessageReceived)]
        assert received
        assert received[0].message.contents == "hello world"
        assert received[0].message.sender_prefixes == "@"

    def test_non_display_rpc_is_dropped(self) -> None:
        _, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[]), frozenset())
        events.clear()
        dispatcher.handle_rpc(RpcCall(signal_name=b"2connectToNetwork(NetworkId)", params=[]))
        assert events == []


class TestBufferRemoval:
    def test_buffer_syncer_remove_emits_buffer_removed(self) -> None:
        state, dispatcher, events = _make_state_and_dispatcher()
        buf = _buffer(10, 1, "#python")
        dispatcher.seed_from_session(_session(network_ids=[1], buffer_infos=[buf]), frozenset())
        events.clear()

        dispatcher.handle_sync(
            SyncMessage(
                class_name=BufferSyncer.CLASS_NAME,
                object_name="",
                slot_name=b"removeBuffer",
                params=[10],
            )
        )
        assert BufferId(10) not in state.buffers
        assert BufferId(10) not in state.messages
        removed = [e for e in events if isinstance(e, BufferRemoved)]
        assert removed and int(removed[0].buffer_id) == 10
        # Subsequent removals don't stack (the dispatcher clears the
        # BufferSyncer's pending-removals set on each slot call).
        events.clear()
        dispatcher.handle_sync(
            SyncMessage(
                class_name=BufferSyncer.CLASS_NAME,
                object_name="",
                slot_name=b"removeBuffer",
                params=[10],
            )
        )
        assert events == []

    def test_rename_buffer_updates_state_and_emits(self) -> None:
        """Regression for codex review finding: renameBuffer was a no-op
        in the dispatcher, leaving stale buffer names in `ClientState`."""
        state, dispatcher, events = _make_state_and_dispatcher()
        buf = _buffer(10, 1, "#oldname")
        dispatcher.seed_from_session(_session(network_ids=[1], buffer_infos=[buf]), frozenset())
        events.clear()

        dispatcher.handle_sync(
            SyncMessage(
                class_name=BufferSyncer.CLASS_NAME,
                object_name="",
                slot_name=b"renameBuffer",
                params=[10, "#newname"],
            )
        )
        # `ClientState.buffers` reflects the new name (BufferInfo is
        # frozen, so the dispatcher rebuilt it).
        assert state.buffers[BufferId(10)].name == "#newname"
        # And a public BufferRenamed event was emitted for UI consumers.
        renamed = [e for e in events if isinstance(e, BufferRenamed)]
        assert len(renamed) == 1
        assert renamed[0].buffer_id == BufferId(10)
        assert renamed[0].name == "#newname"

    def test_merge_buffers_permanently_drops_second_buffer(self) -> None:
        """Regression for codex review finding: mergeBuffersPermanently
        marked the merged-away buffer for removal but the dispatcher
        only drained `removed_buffers` on the literal `removeBuffer`
        slot — so the buffer stayed in `state.buffers` indefinitely."""
        state, dispatcher, events = _make_state_and_dispatcher()
        buf1 = _buffer(10, 1, "#first")
        buf2 = _buffer(11, 1, "#second")
        dispatcher.seed_from_session(
            _session(network_ids=[1], buffer_infos=[buf1, buf2]), frozenset()
        )
        events.clear()

        dispatcher.handle_sync(
            SyncMessage(
                class_name=BufferSyncer.CLASS_NAME,
                object_name="",
                slot_name=b"mergeBuffersPermanently",
                params=[10, 11],
            )
        )
        # buffer 10 (the survivor) is still there, 11 is gone.
        assert BufferId(10) in state.buffers
        assert BufferId(11) not in state.buffers
        assert BufferId(11) not in state.messages
        removed = [e for e in events if isinstance(e, BufferRemoved)]
        assert len(removed) == 1
        assert removed[0].buffer_id == BufferId(11)

    def test_message_retention_cap_drops_oldest(self) -> None:
        """Regression for codex review finding: per-buffer message lists
        grew unbounded, giving a noisy or malicious peer a memory-DoS
        path. The cap is enforced on every displayMsg arrival."""
        state, dispatcher, events = _make_state_and_dispatcher()
        state.max_messages_per_buffer = 3
        buf = _buffer(10, 1, "#flood")
        dispatcher.seed_from_session(
            _session(network_ids=[1], buffer_infos=[buf]), frozenset({"LongTime"})
        )
        events.clear()

        for i in range(5):
            message = Message(
                msg_id=MsgId(i + 1),
                timestamp=dt.datetime(2026, 4, 14, 12, 0, i, tzinfo=dt.UTC),
                type=MessageType.Plain,
                flags=MessageFlag.NONE,
                buffer_info=buf,
                sender="floodbot",
                sender_prefixes="",
                real_name="",
                avatar_url="",
                contents=f"msg {i}",
                peer_features=frozenset({"LongTime"}),
            )
            dispatcher.handle_rpc(RpcCall(signal_name=DISPLAY_MSG_SIGNAL, params=[message]))

        # Cap of 3 → only the last 3 messages survive (msg 2/3/4).
        kept = state.messages[BufferId(10)]
        assert len(kept) == 3
        assert [m.contents for m in kept] == ["msg 2", "msg 3", "msg 4"]
        # Every message still emitted a MessageReceived event — the cap
        # is purely a retention limit, not a delivery filter.
        assert len([e for e in events if isinstance(e, MessageReceived)]) == 5


class TestMidSessionBufferCreation:
    """A buffer that appears mid-session (incoming PM, /join result) has no
    dedicated wire signal — its first displayMsg carries the new
    BufferInfo. The dispatcher must emit BufferAdded for it, or the UI
    never learns the buffer exists and the messages are invisible."""

    def test_display_msg_for_unknown_buffer_emits_buffer_added(self) -> None:
        state, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(
            _session(network_ids=[1], buffer_infos=[]),
            frozenset({"LongTime"}),
        )
        events.clear()
        buf = _buffer(42, 1, "newfriend")
        msg = _make_message(1, buf, "psst, you there?")
        dispatcher.handle_rpc(RpcCall(signal_name=DISPLAY_MSG_SIGNAL, params=[msg]))

        added = [e for e in events if isinstance(e, BufferAdded)]
        assert len(added) == 1
        assert added[0].buffer_id == BufferId(42)
        assert added[0].network_id == NetworkId(1)
        assert added[0].name == "newfriend"
        # BufferAdded must precede MessageReceived so the UI can create
        # the tree node before it routes the message to it.
        kinds = [type(e).__name__ for e in events]
        assert kinds.index("BufferAdded") < kinds.index("MessageReceived")
        assert state.buffers[BufferId(42)].name == "newfriend"

    def test_display_msg_for_known_buffer_does_not_reemit_buffer_added(self) -> None:
        _state, dispatcher, events = _make_state_and_dispatcher()
        buf = _buffer(10, 1, "#python")
        dispatcher.seed_from_session(
            _session(network_ids=[1], buffer_infos=[buf]),
            frozenset({"LongTime"}),
        )
        events.clear()
        msg = _make_message(1, buf, "hello again")
        dispatcher.handle_rpc(RpcCall(signal_name=DISPLAY_MSG_SIGNAL, params=[msg]))
        assert not [e for e in events if isinstance(e, BufferAdded)]


def _make_message(
    msg_id: int,
    buf: BufferInfo,
    contents: str = "hello",
) -> Message:
    return Message(
        msg_id=MsgId(msg_id),
        timestamp=dt.datetime(2026, 4, 14, 12, 0, 0, tzinfo=dt.UTC),
        type=MessageType.Plain,
        flags=MessageFlag.NONE,
        buffer_info=buf,
        sender="nick",
        sender_prefixes="",
        real_name="",
        avatar_url="",
        contents=contents,
        peer_features=frozenset({"LongTime"}),
    )


class TestMergeBacklog:
    def _seed_with_buffer(
        self,
    ) -> tuple[ClientState, Dispatcher, list[ClientEvent], BufferInfo]:
        state, dispatcher, events = _make_state_and_dispatcher()
        buf = _buffer(10, 1, "#python")
        dispatcher.seed_from_session(
            _session(network_ids=[1], buffer_infos=[buf]),
            frozenset({"LongTime"}),
        )
        events.clear()
        return state, dispatcher, events, buf

    def test_basic_backlog_merge(self) -> None:
        state, dispatcher, events, buf = self._seed_with_buffer()
        msgs = [_make_message(i, buf, f"line {i}") for i in range(1, 4)]
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"BacklogManager",
                object_name="",
                slot_name=b"receiveBacklog",
                params=[BufferId(10), MsgId(-1), MsgId(-1), 100, 0, msgs],
            )
        )
        assert len(state.messages[BufferId(10)]) == 3
        bl = [e for e in events if isinstance(e, BacklogReceived)]
        assert len(bl) == 1
        assert bl[0].buffer_id == BufferId(10)
        assert bl[0].count == 3

    def test_backlog_for_removed_buffer_is_dropped(self) -> None:
        """Late backlog reply for a buffer that was removed must not
        resurrect the buffer in state."""
        state, dispatcher, events, buf = self._seed_with_buffer()
        # Remove the buffer via BufferSyncer
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"BufferSyncer",
                object_name="",
                slot_name=b"removeBuffer",
                params=[10],
            )
        )
        events.clear()
        assert BufferId(10) not in state.buffers

        msgs = [_make_message(1, buf, "late backlog")]
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"BacklogManager",
                object_name="",
                slot_name=b"receiveBacklog",
                params=[BufferId(10), MsgId(-1), MsgId(-1), 100, 0, msgs],
            )
        )
        assert BufferId(10) not in state.buffers
        assert BufferId(10) not in state.messages
        bl = [e for e in events if isinstance(e, BacklogReceived)]
        assert len(bl) == 0

    def test_backlog_dedupes_against_existing_and_within_batch(self) -> None:
        """Messages already in state and duplicates within the backlog
        batch itself must not create double entries."""
        state, dispatcher, events, buf = self._seed_with_buffer()
        # Pre-populate with msg_id=2
        existing_msg = _make_message(2, buf, "existing")
        dispatcher.handle_rpc(RpcCall(signal_name=DISPLAY_MSG_SIGNAL, params=[existing_msg]))
        events.clear()
        assert len(state.messages[BufferId(10)]) == 1

        # Backlog: msg_id=1 (new), msg_id=2 (dup of existing),
        # msg_id=3 (new), msg_id=3 (dup within batch)
        batch = [
            _make_message(1, buf, "backlog 1"),
            _make_message(2, buf, "dup of existing"),
            _make_message(3, buf, "backlog 3"),
            _make_message(3, buf, "dup within batch"),
        ]
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"BacklogManager",
                object_name="",
                slot_name=b"receiveBacklog",
                params=[BufferId(10), MsgId(-1), MsgId(-1), 100, 0, batch],
            )
        )
        msgs = state.messages[BufferId(10)]
        assert len(msgs) == 3
        assert [m.contents for m in msgs] == ["backlog 1", "existing", "backlog 3"]

    def test_empty_backlog_reply_emits_backlog_received_zero(self) -> None:
        """An empty reply is still a reply: the UI waits on BacklogReceived
        to know the request completed (e.g. to stop a spinner or decide
        the buffer simply has no history). Silence here looked exactly
        like the known 'backlog never appears' bug."""
        state, dispatcher, events, _buf = self._seed_with_buffer()
        state.backlog_requested.add(BufferId(10))
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"BacklogManager",
                object_name="",
                slot_name=b"receiveBacklog",
                params=[BufferId(10), MsgId(-1), MsgId(-1), 100, 0, []],
            )
        )
        bl = [e for e in events if isinstance(e, BacklogReceived)]
        assert len(bl) == 1
        assert bl[0].buffer_id == BufferId(10)
        assert bl[0].count == 0
        # The reply DID arrive — keep the latch so we don't re-request
        # an empty history on every buffer switch.
        assert BufferId(10) in state.backlog_requested

    def test_malformed_backlog_reply_still_emits_zero_count(self) -> None:
        """A reply whose messages param isn't a list (decode quirk) must
        not silently strand the request: the latch would stay set with
        no event, making backlog permanently unfetchable this session."""
        state, dispatcher, events, _buf = self._seed_with_buffer()
        state.backlog_requested.add(BufferId(10))
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"BacklogManager",
                object_name="",
                slot_name=b"receiveBacklog",
                params=[BufferId(10), MsgId(-1), MsgId(-1), 100, 0, "garbage"],
            )
        )
        bl = [e for e in events if isinstance(e, BacklogReceived)]
        assert len(bl) == 1
        assert bl[0].count == 0

    def test_backlog_reply_for_removed_buffer_clears_latch(self) -> None:
        """A late reply for a removed buffer is discarded, and the latch
        is cleared with it so a re-created buffer can request again."""
        state, dispatcher, events, buf = self._seed_with_buffer()
        state.backlog_requested.add(BufferId(10))
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"BufferSyncer",
                object_name="",
                slot_name=b"removeBuffer",
                params=[10],
            )
        )
        events.clear()
        msgs = [_make_message(1, buf, "late")]
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"BacklogManager",
                object_name="",
                slot_name=b"receiveBacklog",
                params=[BufferId(10), MsgId(-1), MsgId(-1), 100, 0, msgs],
            )
        )
        assert BufferId(10) not in state.backlog_requested

    def test_backlog_mixed_buffer_ids_are_filtered(self) -> None:
        """Messages whose buffer_id doesn't match the slot's authoritative
        buffer_id must be silently dropped."""
        state, dispatcher, _events, buf = self._seed_with_buffer()
        other_buf = _buffer(99, 1, "#other")
        batch = [
            _make_message(1, buf, "correct buffer"),
            _make_message(2, other_buf, "wrong buffer"),
            _make_message(3, buf, "also correct"),
        ]
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"BacklogManager",
                object_name="",
                slot_name=b"receiveBacklog",
                params=[BufferId(10), MsgId(-1), MsgId(-1), 100, 0, batch],
            )
        )
        msgs = state.messages[BufferId(10)]
        assert len(msgs) == 2
        assert [m.contents for m in msgs] == ["correct buffer", "also correct"]


class TestReseedForReconnect:
    def test_seed_clears_stale_backlog_latches(self) -> None:
        """backlog_requested is per-session: re-seeding (reconnect) must
        clear it so the gap since the disconnect gets re-fetched and
        merged (dedup by msg_id makes the re-request safe)."""
        state, dispatcher, _events = _make_state_and_dispatcher()
        state.backlog_requested.add(BufferId(10))
        dispatcher.seed_from_session(
            _session(network_ids=[1], buffer_infos=[_buffer(10, 1, "#python")]),
            frozenset(),
        )
        assert state.backlog_requested == set()

    def test_seed_preserves_existing_message_history(self) -> None:
        """Reconnect re-seeds into the same state — history must survive."""
        state, dispatcher, _events = _make_state_and_dispatcher()
        buf = _buffer(10, 1, "#python")
        dispatcher.seed_from_session(
            _session(network_ids=[1], buffer_infos=[buf]),
            frozenset({"LongTime"}),
        )
        dispatcher.handle_rpc(
            RpcCall(signal_name=DISPLAY_MSG_SIGNAL, params=[_make_message(1, buf, "history")])
        )
        assert len(state.messages[BufferId(10)]) == 1

        dispatcher2 = Dispatcher(state=state, emit=lambda e: None)
        dispatcher2.seed_from_session(
            _session(network_ids=[1], buffer_infos=[buf]),
            frozenset({"LongTime"}),
        )
        assert [m.contents for m in state.messages[BufferId(10)]] == ["history"]


class TestIdentityUserTypeSeed:
    def test_identity_id_usertype_is_accepted(self) -> None:
        """Real cores wrap identityId in the IdentityId user type, which the
        registered codec decodes to the IdentityId dataclass — NOT a plain
        int. The old isinstance(int) guard silently dropped every identity
        from a real core."""
        state, dispatcher, events = _make_state_and_dispatcher()
        session = _session(
            network_ids=[],
            identities=[{"identityId": IdentityId(7), "identityName": "main"}],
        )
        dispatcher.seed_from_session(session, frozenset())
        assert IdentityId(7) in state.identities
        assert state.identities[IdentityId(7)].identity_name == "main"
        assert any(isinstance(e, IdentityAdded) and e.identity_id == IdentityId(7) for e in events)


class TestObjectRenamed:
    """Quassel DOES re-address IrcUser syncables on nick change:
    IrcUser::setNick -> updateObjectName -> renameObject broadcasts the
    __objectRenamed__ RpcCall, and every subsequent Sync frame is
    addressed to "<netId>/<newNick>". Dropping the RPC strands the
    object under its old key and every later update for the user is
    silently lost."""

    def _setup_user_in_channel(self):  # type: ignore[no-untyped-def]
        state, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[1]), frozenset())
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"IrcUser",
                object_name="1/alice",
                slot_name=b"setUser",
                params=["al"],
            )
        )
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"Network",
                object_name="1",
                slot_name=b"addIrcUser",
                params=["alice!al@host"],
            )
        )
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"IrcChannel",
                object_name="1/#chan",
                slot_name=b"joinIrcUsers",
                params=[["alice"], ["@"]],
            )
        )
        events.clear()
        return state, dispatcher, events

    def test_nick_change_rekeys_object_and_rosters(self) -> None:
        state, dispatcher, _events = self._setup_user_in_channel()
        dispatcher.handle_rpc(
            RpcCall(signal_name=b"__objectRenamed__", params=[b"IrcUser", "1/bob", "1/alice"])
        )
        assert dispatcher.get(b"IrcUser", "1/alice") is None
        user = dispatcher.get(b"IrcUser", "1/bob")
        assert isinstance(user, IrcUser)
        assert user.nick == "bob"
        assert user.user == "al"  # same object, fields preserved
        chan = dispatcher.get(b"IrcChannel", "1/#chan")
        assert isinstance(chan, IrcChannel)
        assert chan.user_modes == {"bob": "@"}
        assert "bob" in state.networks[NetworkId(1)].users
        assert "alice" not in state.networks[NetworkId(1)].users

    def test_followup_sync_reaches_renamed_object(self) -> None:
        _state, dispatcher, _events = self._setup_user_in_channel()
        dispatcher.handle_rpc(
            RpcCall(signal_name=b"__objectRenamed__", params=[b"IrcUser", "1/bob", "1/alice"])
        )
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"IrcUser",
                object_name="1/bob",
                slot_name=b"setAway",
                params=[True],
            )
        )
        user = dispatcher.get(b"IrcUser", "1/bob")
        assert isinstance(user, IrcUser)
        assert user.away is True
        assert user.user == "al"


class TestQuitPartCascade:
    """Membership removal arrives via IrcUser only: IrcUser::partChannel
    is SYNCed for parts/kicks and IrcUser::quit for quits; IrcChannel::
    part is NOT a sync method on real cores. The dispatcher must cascade
    these to the channel rosters or every roster grows stale forever."""

    def _setup(self):  # type: ignore[no-untyped-def]
        state, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[1]), frozenset())
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"Network",
                object_name="1",
                slot_name=b"addIrcUser",
                params=["alice!al@host"],
            )
        )
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"IrcUser",
                object_name="1/alice",
                slot_name=b"joinChannel",
                params=["#chan"],
            )
        )
        for chan in ("#chan", "#other"):
            dispatcher.handle_sync(
                SyncMessage(
                    class_name=b"IrcChannel",
                    object_name=f"1/{chan}",
                    slot_name=b"joinIrcUsers",
                    params=[["alice", "bob"], ["@", ""]],
                )
            )
        events.clear()
        return state, dispatcher, events

    def test_part_channel_removes_nick_from_channel_roster(self) -> None:
        _state, dispatcher, _events = self._setup()
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"IrcUser",
                object_name="1/alice",
                slot_name=b"partChannel",
                params=["#chan"],
            )
        )
        chan = dispatcher.get(b"IrcChannel", "1/#chan")
        assert isinstance(chan, IrcChannel)
        assert "alice" not in chan.user_modes
        assert "bob" in chan.user_modes
        # The other channel is untouched.
        other = dispatcher.get(b"IrcChannel", "1/#other")
        assert isinstance(other, IrcChannel)
        assert "alice" in other.user_modes

    def test_quit_removes_user_from_every_roster_and_registry(self) -> None:
        state, dispatcher, _events = self._setup()
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"IrcUser",
                object_name="1/alice",
                slot_name=b"quit",
                params=[],
            )
        )
        for chan_name in ("1/#chan", "1/#other"):
            chan = dispatcher.get(b"IrcChannel", chan_name)
            assert isinstance(chan, IrcChannel)
            assert "alice" not in chan.user_modes
            assert "bob" in chan.user_modes
        assert "alice" not in state.networks[NetworkId(1)].users
        # The syncable is deregistered, as the IrcUser quit docstring
        # has always claimed.
        assert dispatcher.get(b"IrcUser", "1/alice") is None


class TestNetworkLifecycleRpc:
    def test_network_created_registers_placeholder_and_emits(self) -> None:
        state, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[1]), frozenset())
        events.clear()
        dispatcher.handle_rpc(
            RpcCall(signal_name=b"2networkCreated(NetworkId)", params=[NetworkId(7)])
        )
        assert NetworkId(7) in state.networks
        added = [e for e in events if isinstance(e, NetworkAdded)]
        assert len(added) == 1
        assert added[0].network_id == NetworkId(7)

    def test_network_created_for_known_network_is_idempotent(self) -> None:
        _state, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[1]), frozenset())
        events.clear()
        dispatcher.handle_rpc(
            RpcCall(signal_name=b"2networkCreated(NetworkId)", params=[NetworkId(1)])
        )
        assert [e for e in events if isinstance(e, NetworkAdded)] == []

    def test_network_removed_drops_network_buffers_and_messages(self) -> None:
        state, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(
            _session(
                network_ids=[1, 2],
                buffer_infos=[
                    _buffer(10, 1, "#a"),
                    _buffer(11, 1, "#b"),
                    _buffer(20, 2, "#keep"),
                ],
            ),
            frozenset({"LongTime"}),
        )
        dispatcher.handle_rpc(
            RpcCall(
                signal_name=DISPLAY_MSG_SIGNAL,
                params=[_make_message(1, _buffer(10, 1, "#a"), "hello")],
            )
        )
        state.backlog_requested.add(BufferId(10))
        events.clear()

        dispatcher.handle_rpc(
            RpcCall(signal_name=b"2networkRemoved(NetworkId)", params=[NetworkId(1)])
        )
        assert NetworkId(1) not in state.networks
        assert NetworkId(2) in state.networks
        assert BufferId(10) not in state.buffers
        assert BufferId(11) not in state.buffers
        assert BufferId(20) in state.buffers
        assert BufferId(10) not in state.messages
        assert BufferId(10) not in state.backlog_requested
        removed = {e.buffer_id for e in events if isinstance(e, BufferRemoved)}
        assert removed == {BufferId(10), BufferId(11)}
        network_removed = [e for e in events if isinstance(e, NetworkRemoved)]
        assert len(network_removed) == 1
        assert network_removed[0].network_id == NetworkId(1)
        assert dispatcher.get(b"Network", "1") is None


class TestStructOfArraysSeed:
    def test_parallel_array_users_and_channels_seed(self) -> None:
        """Real cores (>= 0.10) ship IrcUsersAndChannels as struct-of-
        arrays: each of Users/Channels is a map of attribute-name ->
        parallel QVariantList. The old parser assumed per-nick dicts and
        its isinstance(v, dict) filter dropped everything, yielding an
        empty roster against every real core."""
        state, dispatcher, _events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[3]), frozenset())
        init = InitData(
            class_name=b"Network",
            object_name="3",
            init_data={
                "networkName": "libera",
                "IrcUsersAndChannels": {
                    "Users": {
                        "nick": ["alice", "bob"],
                        "user": ["al", "bo"],
                        "host": ["h1", "h2"],
                        "away": [False, True],
                    },
                    "Channels": {
                        "name": ["#chan"],
                        "topic": ["greetings"],
                        "UserModes": [{"alice": "@", "bob": ""}],
                    },
                },
            },
        )
        dispatcher.handle_init_data(init)
        alice = dispatcher.get(b"IrcUser", "3/alice")
        assert isinstance(alice, IrcUser)
        assert (alice.user, alice.host, alice.away) == ("al", "h1", False)
        bob = dispatcher.get(b"IrcUser", "3/bob")
        assert isinstance(bob, IrcUser)
        assert bob.away is True
        chan = dispatcher.get(b"IrcChannel", "3/#chan")
        assert isinstance(chan, IrcChannel)
        assert chan.topic == "greetings"
        assert chan.user_modes == {"alice": "@", "bob": ""}
        net = state.networks[NetworkId(3)]
        assert net.users == {"alice", "bob"}
        assert net.channels == {"#chan"}


class TestMarkerSeedFromCore:
    def test_marker_lines_init_seeds_read_markers(self) -> None:
        """The core persists marker lines across sessions; seeding
        state.read_markers from BufferSyncer InitData is what makes a
        marker placed in a previous run (or another client) show up."""
        state, dispatcher, _events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(
            _session(network_ids=[1], buffer_infos=[_buffer(10, 1, "#a")]),
            frozenset(),
        )
        init = InitData(
            class_name=b"BufferSyncer",
            object_name="",
            init_data={"MarkerLines": {"10": 42}},
        )
        dispatcher.handle_init_data(init)
        assert state.read_markers.get(BufferId(10)) == MsgId(42)

    def test_marker_seed_does_not_clobber_local_placement(self) -> None:
        state, dispatcher, _events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(
            _session(network_ids=[1], buffer_infos=[_buffer(10, 1, "#a")]),
            frozenset(),
        )
        state.read_markers[BufferId(10)] = MsgId(99)
        init = InitData(
            class_name=b"BufferSyncer",
            object_name="",
            init_data={"MarkerLines": {"10": 42}},
        )
        dispatcher.handle_init_data(init)
        assert state.read_markers[BufferId(10)] == MsgId(99)


class TestBranchReviewHardening:
    def test_malformed_backlog_buffer_id_is_dropped_not_typeerror(self) -> None:
        """A receiveBacklog whose buffer_id param is garbage used to raise
        TypeError from BufferId(int(...)) — past every (OSError,
        QuasselError) net, crashing the bridge worker."""
        _state, dispatcher, events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(
            _session(network_ids=[1], buffer_infos=[_buffer(10, 1, "#a")]),
            frozenset(),
        )
        events.clear()
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"BacklogManager",
                object_name="",
                slot_name=b"receiveBacklog",
                params=[{"not": "an id"}, MsgId(-1), MsgId(-1), 100, 0, []],
            )
        )
        assert [e for e in events if isinstance(e, BacklogReceived)] == []

    def test_rename_for_uninstantiated_user_still_rekeys_rosters(self) -> None:
        """A nick can be in channel rosters (via joinIrcUsers / the init
        seed) without an IrcUser syncable ever existing. Dropping the
        __objectRenamed__ in that case ghosts the old nick forever."""
        state, dispatcher, _events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[1]), frozenset())
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"Network",
                object_name="1",
                slot_name=b"addIrcUser",
                params=["alice!a@h"],
            )
        )
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"IrcChannel",
                object_name="1/#chan",
                slot_name=b"joinIrcUsers",
                params=[["alice"], ["@"]],
            )
        )
        assert dispatcher.get(b"IrcUser", "1/alice") is None  # never instantiated
        dispatcher.handle_rpc(
            RpcCall(signal_name=b"__objectRenamed__", params=[b"IrcUser", "1/bob", "1/alice"])
        )
        chan = dispatcher.get(b"IrcChannel", "1/#chan")
        assert isinstance(chan, IrcChannel)
        assert chan.user_modes == {"bob": "@"}
        net = state.networks[NetworkId(1)]
        assert "bob" in net.users and "alice" not in net.users

    def test_network_disconnect_clears_rosters(self) -> None:
        """Network::setConnected(false) mirrors the C++ client's
        removeChansAndUsers(): without it every IRC-side disconnect
        leaves permanently stale rosters that merge ghosts on
        reconnect."""
        state, dispatcher, _events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[1]), frozenset())
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"Network",
                object_name="1",
                slot_name=b"addIrcUser",
                params=["alice!a@h"],
            )
        )
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"IrcChannel",
                object_name="1/#chan",
                slot_name=b"joinIrcUsers",
                params=[["alice"], ["@"]],
            )
        )
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"IrcUser", object_name="1/alice", slot_name=b"setUser", params=["a"]
            )
        )
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"Network", object_name="1", slot_name=b"setConnected", params=[False]
            )
        )
        net = state.networks[NetworkId(1)]
        assert net.users == set()
        assert net.channels == set()
        assert dispatcher.get(b"IrcUser", "1/alice") is None
        assert dispatcher.get(b"IrcChannel", "1/#chan") is None

    def test_own_part_tears_down_the_channel(self) -> None:
        """When WE part a channel, the core stops syncing it — keeping
        the old roster means ghost members merge in on rejoin."""
        state, dispatcher, _events = _make_state_and_dispatcher()
        dispatcher.seed_from_session(_session(network_ids=[1]), frozenset())
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"Network", object_name="1", slot_name=b"setMyNick", params=["me"]
            )
        )
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"IrcChannel",
                object_name="1/#chan",
                slot_name=b"joinIrcUsers",
                params=[["me", "alice"], ["", "@"]],
            )
        )
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"Network", object_name="1", slot_name=b"addIrcChannel", params=["#chan"]
            )
        )
        dispatcher.handle_sync(
            SyncMessage(
                class_name=b"IrcUser",
                object_name="1/me",
                slot_name=b"partChannel",
                params=["#chan"],
            )
        )
        assert dispatcher.get(b"IrcChannel", "1/#chan") is None
        net = state.networks[NetworkId(1)]
        assert "#chan" not in net.channels
