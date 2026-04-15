"""Unit tests for the QuasselConnection state machine.

We don't open a real TCP socket in unit tests. Instead we stub out the
transport primitives (`open_tcp_connection`, `probe`, and `start_tls_on_writer`)
and hand the connection a `FakeStream` pair pre-loaded with the byte sequence
we want it to see. That exercises the full state machine — handshake
messages, TLS fail-closed, heartbeat auto-reply, SessionInit decoding — against
fixed bytes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest

from quasseltui.protocol.connection import (
    ConnState,
    Disconnected,
    HeartBeatEvent,
    QuasselConnection,
    RpcEvent,
    SessionReady,
    SyncEvent,
)
from quasseltui.protocol.enums import FEATURE_LONG_TIME, FEATURE_RICH_MESSAGES
from quasseltui.protocol.framing import encode_frame
from quasseltui.protocol.handshake import encode_handshake_payload
from quasseltui.protocol.messages import (
    CLIENT_INIT_ACK,
    CLIENT_LOGIN_ACK,
    CLIENT_LOGIN_REJECT,
    SESSION_INIT,
)
from quasseltui.protocol.probe import (
    ConnectionFeature,
    NegotiatedProtocol,
    ProtocolType,
)
from quasseltui.protocol.signalproxy import (
    HeartBeat,
    HeartBeatReply,
    RpcCall,
    SyncMessage,
    decode_signalproxy_payload,
    encode_signalproxy_payload,
)

# ---------------------------------------------------------------------------
# FakeStream: a byte-backed reader/writer pair that replays prepared bytes
# and captures everything the connection writes. Only implements the methods
# the connection actually uses.
# ---------------------------------------------------------------------------


class FakeStream:
    def __init__(self, inbound: bytes) -> None:
        self._inbound = inbound
        self._pos = 0
        self.written = bytearray()
        self.closed = False

    # --- StreamReader surface ---

    async def readexactly(self, n: int) -> bytes:
        if self._pos + n > len(self._inbound):
            raise asyncio.IncompleteReadError(
                partial=self._inbound[self._pos :],
                expected=n,
            )
        chunk = self._inbound[self._pos : self._pos + n]
        self._pos += n
        return chunk

    # --- StreamWriter surface ---

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    async def start_tls(self, *_args: Any, **_kwargs: Any) -> None:
        # Pretend the upgrade succeeded. The connection's own logic has
        # already read the probe reply bytes we seeded.
        return None


# ---------------------------------------------------------------------------
# Builders for the handshake + streaming byte sequences.
# ---------------------------------------------------------------------------


def _framed_map(data: dict[str, Any]) -> bytes:
    return encode_frame(encode_handshake_payload(data))


def _framed_signalproxy(msg: Any, features: frozenset[str]) -> bytes:
    return encode_frame(encode_signalproxy_payload(msg, peer_features=features))


def _build_inbound(
    *,
    init_ack: dict[str, Any],
    login_ack: dict[str, Any],
    session_init: dict[str, Any],
    signalproxy_frames: list[bytes] | None = None,
) -> bytes:
    frames = signalproxy_frames or []
    return (
        _framed_map(init_ack)
        + _framed_map(login_ack)
        + _framed_map(session_init)
        + b"".join(frames)
    )


def _base_init_ack() -> dict[str, Any]:
    return {
        "MsgType": CLIENT_INIT_ACK,
        "Configured": True,
        "CoreFeatures": 0,
        "FeatureList": [FEATURE_LONG_TIME, FEATURE_RICH_MESSAGES],
        "StorageBackends": [],
    }


def _base_login_ack() -> dict[str, Any]:
    return {"MsgType": CLIENT_LOGIN_ACK}


def _base_session_init() -> dict[str, Any]:
    return {
        "MsgType": SESSION_INIT,
        "SessionState": {
            "Identities": [],
            "NetworkIds": [],
            "BufferInfos": [],
        },
    }


# ---------------------------------------------------------------------------
# Patching helpers — replace the transport primitives for the duration of
# a single test.
# ---------------------------------------------------------------------------


class _FakeConnectionContext:
    def __init__(self, inbound: bytes, *, tls_offered: bool, tls_enabled: bool) -> None:
        self.stream = FakeStream(inbound)
        self.tls_offered = tls_offered
        self.tls_enabled = tls_enabled
        self.tls_upgrade_called = False

    async def open(self, *_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return self.stream, self.stream

    async def probe(
        self,
        _reader: Any,
        _writer: Any,
        *,
        offered_features: ConnectionFeature = ConnectionFeature.NONE,
    ) -> NegotiatedProtocol:
        features = ConnectionFeature.Encryption if self.tls_enabled else ConnectionFeature.NONE
        return NegotiatedProtocol(
            protocol=ProtocolType.DataStream,
            peer_features=0,
            connection_features=features,
        )

    async def start_tls(self, *_args: Any, **_kwargs: Any) -> None:
        self.tls_upgrade_called = True


@pytest.fixture
def patched_transport(monkeypatch: pytest.MonkeyPatch):
    """Return a factory that installs the fake primitives and hands back
    the context for assertions."""

    def _install(inbound: bytes, *, tls_offered: bool, tls_enabled: bool) -> _FakeConnectionContext:
        ctx = _FakeConnectionContext(
            inbound,
            tls_offered=tls_offered,
            tls_enabled=tls_enabled,
        )
        import quasseltui.protocol.connection as mod

        monkeypatch.setattr(mod, "open_tcp_connection", ctx.open)
        monkeypatch.setattr(mod, "probe", ctx.probe)
        monkeypatch.setattr(mod, "start_tls_on_writer", ctx.start_tls)
        return ctx

    return _install


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHandshakeSuccess:
    @pytest.mark.asyncio
    async def test_session_ready_is_first_event(self, patched_transport) -> None:
        inbound = _build_inbound(
            init_ack=_base_init_ack(),
            login_ack=_base_login_ack(),
            session_init=_base_session_init(),
            signalproxy_frames=[],
        )
        patched_transport(inbound, tls_offered=False, tls_enabled=False)

        conn = QuasselConnection(
            host="core",
            port=4242,
            user="u",
            password="p",
            tls=False,
        )

        events = []
        async for event in conn.events():
            events.append(event)

        assert isinstance(events[0], SessionReady)
        assert isinstance(events[-1], Disconnected)
        assert events[0].peer_features == frozenset({FEATURE_LONG_TIME, FEATURE_RICH_MESSAGES})
        assert conn.state is ConnState.CLOSED

    @pytest.mark.asyncio
    async def test_peer_features_are_intersection(self, patched_transport) -> None:
        """The negotiated feature set is the intersection of what we
        offered and what the core advertised — not the union."""
        ack = _base_init_ack()
        # Core advertises LongTime and RichMessages; we offer all defaults.
        # Intersection should be exactly what the core has.
        ack["FeatureList"] = [FEATURE_LONG_TIME]
        inbound = _build_inbound(
            init_ack=ack,
            login_ack=_base_login_ack(),
            session_init=_base_session_init(),
        )
        patched_transport(inbound, tls_offered=False, tls_enabled=False)

        conn = QuasselConnection(
            host="core",
            port=4242,
            user="u",
            password="p",
            tls=False,
        )
        events = [e async for e in conn.events()]
        ready = events[0]
        assert isinstance(ready, SessionReady)
        assert ready.peer_features == frozenset({FEATURE_LONG_TIME})


class TestTlsDowngrade:
    @pytest.mark.asyncio
    async def test_offered_tls_but_core_declined_aborts_before_init(
        self,
        patched_transport,
    ) -> None:
        """The state machine MUST refuse to send ClientInit (and hence
        ClientLogin) if we asked for TLS and the core refused. The fake
        inbound bytes are fine, but we should bail during the handshake
        without ever reading them."""
        inbound = _build_inbound(
            init_ack=_base_init_ack(),
            login_ack=_base_login_ack(),
            session_init=_base_session_init(),
        )
        ctx = patched_transport(inbound, tls_offered=True, tls_enabled=False)

        conn = QuasselConnection(
            host="core",
            port=4242,
            user="u",
            password="p",
            tls=True,
        )
        events = [e async for e in conn.events()]
        assert len(events) == 1
        assert isinstance(events[0], Disconnected)
        reason_lower = events[0].reason.lower()
        assert "tls" in reason_lower
        assert "plaintext" in reason_lower
        # Crucially: we never wrote anything to the stream, because ClientInit
        # is the next step and we aborted before it.
        assert ctx.stream.written == b""

    @pytest.mark.asyncio
    async def test_tls_enabled_triggers_start_tls(self, patched_transport) -> None:
        inbound = _build_inbound(
            init_ack=_base_init_ack(),
            login_ack=_base_login_ack(),
            session_init=_base_session_init(),
        )
        ctx = patched_transport(inbound, tls_offered=True, tls_enabled=True)

        conn = QuasselConnection(
            host="core",
            port=4242,
            user="u",
            password="p",
            tls=True,
        )
        events = [e async for e in conn.events()]
        ready_events = [e for e in events if isinstance(e, SessionReady)]
        assert len(ready_events) == 1
        assert ctx.tls_upgrade_called


class TestAuthFailure:
    @pytest.mark.asyncio
    async def test_login_reject_surfaces_as_disconnected(self, patched_transport) -> None:
        inbound = _build_inbound(
            init_ack=_base_init_ack(),
            login_ack={"MsgType": CLIENT_LOGIN_REJECT, "Error": "bad password"},
            session_init=_base_session_init(),
        )
        patched_transport(inbound, tls_offered=False, tls_enabled=False)

        conn = QuasselConnection(
            host="core",
            port=4242,
            user="u",
            password="wrong",
            tls=False,
        )
        events = [e async for e in conn.events()]
        assert len(events) == 1
        assert isinstance(events[0], Disconnected)
        assert "auth" in events[0].reason.lower()
        assert "bad password" in events[0].reason


class TestConnectedLoop:
    @pytest.mark.asyncio
    async def test_heartbeat_auto_reply(self, patched_transport) -> None:
        features = frozenset({FEATURE_LONG_TIME, FEATURE_RICH_MESSAGES})
        ts = dt.datetime(2026, 4, 14, 12, 34, 56, tzinfo=dt.UTC)
        inbound = _build_inbound(
            init_ack=_base_init_ack(),
            login_ack=_base_login_ack(),
            session_init=_base_session_init(),
            signalproxy_frames=[_framed_signalproxy(HeartBeat(timestamp=ts), features)],
        )
        ctx = patched_transport(inbound, tls_offered=False, tls_enabled=False)

        conn = QuasselConnection(
            host="core",
            port=4242,
            user="u",
            password="p",
            tls=False,
        )
        events = [e async for e in conn.events()]
        heartbeat_events = [e for e in events if isinstance(e, HeartBeatEvent)]
        assert len(heartbeat_events) == 1
        assert heartbeat_events[0].message.timestamp == ts

        # The connection must have written back a HeartBeatReply before
        # yielding the event — inspect the captured bytes to confirm.
        sent_frames = _split_frames(bytes(ctx.stream.written))
        # Writes are: ClientInit, ClientLogin, HeartBeatReply (3 total).
        assert len(sent_frames) == 3
        hb_reply = decode_signalproxy_payload(sent_frames[-1], peer_features=features)
        assert isinstance(hb_reply, HeartBeatReply)
        assert hb_reply.timestamp == ts

    @pytest.mark.asyncio
    async def test_sync_rpc_events_yield_in_order(self, patched_transport) -> None:
        features = frozenset({FEATURE_LONG_TIME, FEATURE_RICH_MESSAGES})
        sync = SyncMessage(
            class_name=b"Network",
            object_name="1",
            slot_name=b"setNetworkName",
            params=["freenode"],
        )
        rpc = RpcCall(signal_name=b"2test()", params=[])
        inbound = _build_inbound(
            init_ack=_base_init_ack(),
            login_ack=_base_login_ack(),
            session_init=_base_session_init(),
            signalproxy_frames=[
                _framed_signalproxy(sync, features),
                _framed_signalproxy(rpc, features),
            ],
        )
        patched_transport(inbound, tls_offered=False, tls_enabled=False)

        conn = QuasselConnection(
            host="core",
            port=4242,
            user="u",
            password="p",
            tls=False,
        )
        events = [e async for e in conn.events()]
        # Expected order: SessionReady, SyncEvent, RpcEvent, Disconnected
        assert isinstance(events[0], SessionReady)
        assert isinstance(events[1], SyncEvent)
        assert events[1].message == sync
        assert isinstance(events[2], RpcEvent)
        assert events[2].message == rpc
        assert isinstance(events[3], Disconnected)

    @pytest.mark.asyncio
    async def test_heartbeat_reply_failure_yields_disconnected(
        self,
        patched_transport,
    ) -> None:
        """Regression for a codex finding: if the HeartBeatReply write
        fails (e.g. broken pipe), the connection MUST tear down and
        yield a terminal Disconnected rather than swallow the error and
        return a healthy HeartBeatEvent. A caller seeing a HeartBeatEvent
        should be able to trust that the socket is still alive.
        """
        features = frozenset({FEATURE_LONG_TIME, FEATURE_RICH_MESSAGES})
        ts = dt.datetime(2026, 4, 14, 12, 34, 56, tzinfo=dt.UTC)
        inbound = _build_inbound(
            init_ack=_base_init_ack(),
            login_ack=_base_login_ack(),
            session_init=_base_session_init(),
            signalproxy_frames=[_framed_signalproxy(HeartBeat(timestamp=ts), features)],
        )
        patched_transport(inbound, tls_offered=False, tls_enabled=False)

        conn = QuasselConnection(
            host="core",
            port=4242,
            user="u",
            password="p",
            tls=False,
        )

        # Patch the writer to blow up on the HeartBeatReply write. The
        # first two writes (ClientInit + ClientLogin) must go through
        # so the handshake finishes; only the third (HeartBeatReply)
        # fails, simulating a peer that closes the socket mid-session.
        original_send = conn._send_signalproxy
        send_calls = 0

        async def flaky_send(message):  # type: ignore[no-untyped-def]
            nonlocal send_calls
            send_calls += 1
            if send_calls == 1:
                raise OSError("broken pipe")
            return await original_send(message)

        conn._send_signalproxy = flaky_send  # type: ignore[method-assign]

        events = [e async for e in conn.events()]
        # No HeartBeatEvent should be yielded — the loop must tear down
        # before returning a "healthy" event.
        assert not any(isinstance(e, HeartBeatEvent) for e in events)
        # The last event is a Disconnected carrying the OSError.
        last = events[-1]
        assert isinstance(last, Disconnected)
        assert isinstance(last.error, OSError)
        assert "broken pipe" in last.reason
        assert conn.state is ConnState.CLOSED

    @pytest.mark.asyncio
    async def test_events_can_only_be_iterated_once(self, patched_transport) -> None:
        inbound = _build_inbound(
            init_ack=_base_init_ack(),
            login_ack=_base_login_ack(),
            session_init=_base_session_init(),
        )
        patched_transport(inbound, tls_offered=False, tls_enabled=False)

        conn = QuasselConnection(
            host="core",
            port=4242,
            user="u",
            password="p",
            tls=False,
        )
        # First iteration is fine.
        _ = [e async for e in conn.events()]
        # Second raises because events() is one-shot.
        with pytest.raises(RuntimeError, match="may only be called once"):
            async for _ in conn.events():
                pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_frames(buffer: bytes) -> list[bytes]:
    """Split a bytes buffer into framed payloads. Assumes no trailing bytes."""
    out: list[bytes] = []
    pos = 0
    while pos < len(buffer):
        length = int.from_bytes(buffer[pos : pos + 4], "big")
        pos += 4
        out.append(buffer[pos : pos + length])
        pos += length
    return out
