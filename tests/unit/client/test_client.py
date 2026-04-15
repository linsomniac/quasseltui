"""Unit tests for `QuasselClient.events()` transform.

The client is mostly glue between the protocol-layer `QuasselConnection`
and the sync-layer dispatcher, so these tests use a `FakeConnection` that
yields canned `ProtocolEvent`s and records outbound `SignalProxy` messages.
The real `QuasselConnection` is constructed by `QuasselClient.__init__`,
but we swap it out on the instance before calling `events()`.

Things worth pinning:

- The first client event is `SessionOpened`.
- `SessionReady` triggers an InitRequest fan-out — one for BufferSyncer,
  plus one per network id in the SessionInit.
- A `SyncEvent` / `InitDataEvent` / `RpcEvent` from the connection gets
  routed through the dispatcher and its emitted events bubble up.
- A protocol `Disconnected` converts into a terminal
  `client.events.Disconnected` and stops iteration.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

from quasseltui.client.client import QuasselClient
from quasseltui.client.events import (
    BufferAdded,
    Disconnected,
    MessageReceived,
    NetworkAdded,
    NetworkUpdated,
    SessionOpened,
)
from quasseltui.protocol.connection import (
    Disconnected as ProtoDisconnected,
)
from quasseltui.protocol.connection import (
    HeartBeatEvent,
    InitDataEvent,
    ProtocolEvent,
    RpcEvent,
    SessionReady,
    SyncEvent,
)
from quasseltui.protocol.enums import MessageFlag, MessageType
from quasseltui.protocol.messages import ClientInitAck, SessionInit
from quasseltui.protocol.signalproxy import (
    HeartBeat,
    InitData,
    InitRequest,
    RpcCall,
    SignalProxyMessage,
    SyncMessage,
)
from quasseltui.protocol.usertypes import (
    BufferId,
    BufferInfo,
    BufferType,
    Message,
    MsgId,
    NetworkId,
)
from quasseltui.sync.dispatcher import DISPLAY_MSG_SIGNAL


class FakeConnection:
    """Scripted stand-in for `QuasselConnection`.

    Yields a canned sequence of `ProtocolEvent`s from `events()` and
    records every `send()` call for assertions. `close()` is a no-op.

    Subtlety: the client's event loop will `await` between yields (e.g.
    to fan out InitRequests), so our `events()` has to be a real async
    generator — not just a list iterator — otherwise the awaits can
    interleave in unexpected ways.
    """

    def __init__(self, script: list[ProtocolEvent]) -> None:
        self._script = script
        self.sent: list[SignalProxyMessage] = []
        self.closed = False

    async def events(self) -> AsyncIterator[ProtocolEvent]:
        for event in self._script:
            yield event

    async def send(self, message: SignalProxyMessage) -> None:
        self.sent.append(message)

    async def close(self, *, reason: str = "") -> None:
        self.closed = True


def _buffer(buffer_id: int, network_id: int, name: str) -> BufferInfo:
    return BufferInfo(
        buffer_id=BufferId(buffer_id),
        network_id=NetworkId(network_id),
        type=BufferType.Channel,
        group_id=0,
        name=name,
    )


def _session(
    *,
    network_ids: list[int] | None = None,
    buffer_infos: list[BufferInfo] | None = None,
) -> SessionInit:
    net_ids = [1] if network_ids is None else network_ids
    bufs: list[BufferInfo] = [] if buffer_infos is None else buffer_infos
    return SessionInit(
        identities=(),
        network_ids=tuple(NetworkId(i) for i in net_ids),
        buffer_infos=tuple(bufs),
        raw={"SessionState": {}},
    )


def _init_ack() -> ClientInitAck:
    return ClientInitAck(
        core_features=0,
        feature_list=(),
        configured=True,
        storage_backends=(),
        authenticators=(),
        protocol_version=1,
        raw={},
    )


def _session_ready(
    session: SessionInit,
    features: frozenset[str] = frozenset(),
) -> SessionReady:
    return SessionReady(
        session=session,
        peer_features=features,
        core_init_ack=_init_ack(),
    )


def _make_client(script: list[ProtocolEvent]) -> tuple[QuasselClient, FakeConnection]:
    client = QuasselClient(
        host="localhost",
        port=4242,
        user="test",
        password="test",
        tls=False,
    )
    fake = FakeConnection(script)
    # Swap out the real connection. `QuasselClient._connection` is the only
    # field that needs replacing — the dispatcher and state already exist.
    client._connection = fake  # type: ignore[assignment]
    return client, fake


async def _drain(client: QuasselClient) -> list[Any]:
    return [event async for event in client.events()]


class TestSessionFanout:
    async def test_session_ready_triggers_init_request_fanout(self) -> None:
        session = _session(network_ids=[1, 5])
        script: list[ProtocolEvent] = [
            _session_ready(session, frozenset({"LongTime"})),
            ProtoDisconnected(reason="done"),
        ]
        client, fake = _make_client(script)
        events = await _drain(client)

        # SessionOpened is the first event the consumer sees; then
        # NetworkAdded / BufferAdded for everything in SessionInit; then
        # Disconnected terminates.
        assert isinstance(events[0], SessionOpened)
        assert any(isinstance(e, NetworkAdded) for e in events)
        assert isinstance(events[-1], Disconnected)

        # Exactly one BufferSyncer + one InitRequest per network.
        init_requests = [m for m in fake.sent if isinstance(m, InitRequest)]
        classes = [(r.class_name, r.object_name) for r in init_requests]
        assert (b"BufferSyncer", "") in classes
        assert (b"Network", "1") in classes
        assert (b"Network", "5") in classes

    async def test_disconnected_event_is_terminal(self) -> None:
        script: list[ProtocolEvent] = [
            _session_ready(_session(network_ids=[]), frozenset()),
            ProtoDisconnected(reason="bye"),
            # If the iterator didn't stop at Disconnected, this would leak
            # through as a client event too.
            _session_ready(_session(network_ids=[]), frozenset()),
        ]
        client, _ = _make_client(script)
        events = await _drain(client)
        # Find the Disconnected event and make sure nothing comes after it.
        disconnected_idx = next(i for i, e in enumerate(events) if isinstance(e, Disconnected))
        assert disconnected_idx == len(events) - 1

    async def test_fanout_send_failure_yields_terminal_disconnected(self) -> None:
        """Regression for codex review finding: an OSError from the
        InitRequest fan-out used to bubble out of `events()` as an
        uncaught exception, breaking the "always yields one terminal
        Disconnected" contract. The client now catches it and converts."""
        from quasseltui.protocol.errors import QuasselError as _QuasselError

        class _BrokenSendConnection(FakeConnection):
            async def send(self, message: SignalProxyMessage) -> None:
                raise _QuasselError("simulated broken pipe")

        script: list[ProtocolEvent] = [
            _session_ready(_session(network_ids=[1, 2]), frozenset()),
        ]
        client = QuasselClient(
            host="localhost",
            port=4242,
            user="t",
            password="t",
            tls=False,
        )
        broken = _BrokenSendConnection(script)
        client._connection = broken  # type: ignore[assignment]

        events = await _drain(client)
        # The async iterator must NOT have raised — that's what the bug
        # was. The terminal event is a Disconnected carrying the cause.
        assert isinstance(events[-1], Disconnected)
        assert "fan-out" in events[-1].reason
        assert isinstance(events[-1].error, _QuasselError)
        # And the connection was closed as part of the conversion.
        assert broken.closed is True


class TestHandleProtocolEvents:
    async def test_sync_event_mutates_state_and_emits_network_updated(self) -> None:
        session = _session(network_ids=[1])
        sync_msg = SyncMessage(
            class_name=b"Network",
            object_name="1",
            slot_name=b"setNetworkName",
            params=["freenode"],
        )
        script: list[ProtocolEvent] = [
            _session_ready(session, frozenset()),
            SyncEvent(message=sync_msg),
            ProtoDisconnected(reason="done"),
        ]
        client, _ = _make_client(script)
        events = await _drain(client)
        assert client.state.networks[NetworkId(1)].network_name == "freenode"
        updates = [e for e in events if isinstance(e, NetworkUpdated)]
        assert updates and updates[-1].value == "freenode"

    async def test_init_data_event_applies_and_emits(self) -> None:
        session = _session(network_ids=[3])
        init = InitData(
            class_name=b"Network",
            object_name="3",
            init_data={"networkName": "rizon", "myNick": "seanr"},
        )
        script: list[ProtocolEvent] = [
            _session_ready(session, frozenset()),
            InitDataEvent(message=init),
            ProtoDisconnected(reason="done"),
        ]
        client, _ = _make_client(script)
        await _drain(client)
        assert client.state.networks[NetworkId(3)].network_name == "rizon"
        assert client.state.networks[NetworkId(3)].my_nick == "seanr"

    async def test_rpc_display_msg_emits_message_received(self) -> None:
        buf = _buffer(10, 1, "#python")
        session = _session(network_ids=[1], buffer_infos=[buf])
        message = Message(
            msg_id=MsgId(123),
            timestamp=dt.datetime(2026, 4, 14, 12, 0, tzinfo=dt.UTC),
            type=MessageType.Plain,
            flags=MessageFlag.NONE,
            buffer_info=buf,
            sender="seanr",
            sender_prefixes="",
            real_name="",
            avatar_url="",
            contents="hello",
            peer_features=frozenset(),
        )
        script: list[ProtocolEvent] = [
            _session_ready(session, frozenset()),
            RpcEvent(message=RpcCall(signal_name=DISPLAY_MSG_SIGNAL, params=[message])),
            ProtoDisconnected(reason="done"),
        ]
        client, _ = _make_client(script)
        events = await _drain(client)
        received = [e for e in events if isinstance(e, MessageReceived)]
        assert received and received[0].message.contents == "hello"
        assert len(client.state.messages[BufferId(10)]) == 1

    async def test_heartbeat_event_is_swallowed(self) -> None:
        session = _session(network_ids=[])
        hb_ts = dt.datetime(2026, 4, 14, tzinfo=dt.UTC)
        script: list[ProtocolEvent] = [
            _session_ready(session, frozenset()),
            HeartBeatEvent(message=HeartBeat(timestamp=hb_ts)),
            ProtoDisconnected(reason="done"),
        ]
        client, _ = _make_client(script)
        events = await _drain(client)
        # The heartbeat produced no public event — only SessionOpened +
        # terminal Disconnected should be visible.
        kinds = [type(e).__name__ for e in events]
        assert "HeartBeatEvent" not in kinds


class TestBufferSeeding:
    async def test_session_buffers_become_buffer_added_events(self) -> None:
        buf_a = _buffer(10, 1, "#python")
        buf_b = _buffer(11, 1, "#rust")
        session = _session(network_ids=[1], buffer_infos=[buf_a, buf_b])
        script: list[ProtocolEvent] = [
            _session_ready(session, frozenset()),
            ProtoDisconnected(reason="done"),
        ]
        client, _ = _make_client(script)
        events = await _drain(client)
        added = [e for e in events if isinstance(e, BufferAdded)]
        assert {a.name for a in added} == {"#python", "#rust"}
        assert client.state.buffers[BufferId(10)].name == "#python"


class TestSendInput:
    """Phase 9: outbound `sendInput` path — UI → core."""

    async def test_send_input_emits_expected_rpc_call(self) -> None:
        """`send_input` must produce a `RpcCall` with the Quassel-signature
        `signalName` and the full `BufferInfo` as the first parameter.

        Regression guard for two easy-to-make mistakes: (a) sending just
        the `BufferId` int (which the core would reject because the
        slot signature expects a `BufferInfo`), and (b) using the wrong
        signal-name prefix or argument spelling, which core would also
        silently drop.
        """
        buf = _buffer(10, 1, "#python")
        session = _session(network_ids=[1], buffer_infos=[buf])
        script: list[ProtocolEvent] = [_session_ready(session, frozenset())]
        client, fake = _make_client(script)
        # Drain the session-ready fan-out so state.buffers is populated.
        await _drain(client)
        fake.sent.clear()

        await client.send_input(buf.buffer_id, "hello world")

        rpc_calls = [m for m in fake.sent if isinstance(m, RpcCall)]
        assert len(rpc_calls) == 1
        rpc = rpc_calls[0]
        assert rpc.signal_name == b"2sendInput(BufferInfo,QString)"
        assert len(rpc.params) == 2
        assert rpc.params[0] == buf  # full BufferInfo, not bare id
        assert rpc.params[1] == "hello world"

    async def test_send_input_raises_for_unknown_buffer(self) -> None:
        """Racey deletion: the user hits Enter on a buffer that was just
        removed by the core. Without the guard we'd write a `RpcCall`
        carrying `None` through the variant encoder and blow up there;
        catching it at the public entry point lets the UI surface it
        as a non-fatal warning instead.
        """
        from quasseltui.protocol.errors import QuasselError as _QuasselError

        session = _session(network_ids=[1])
        script: list[ProtocolEvent] = [_session_ready(session, frozenset())]
        client, _ = _make_client(script)
        await _drain(client)

        import pytest

        with pytest.raises(_QuasselError):
            await client.send_input(BufferId(9999), "ghost")
