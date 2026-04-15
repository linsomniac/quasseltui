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
    BufferAdded,
    BufferRemoved,
    ClientEvent,
    IdentityAdded,
    MessageReceived,
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
