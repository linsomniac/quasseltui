"""Quassel connection state machine: PROBING -> HANDSHAKE -> CONNECTED.

This is the contract that everything above it (the sync layer, the embeddable
client, eventually the Textual UI) consumes. A `QuasselConnection` owns one
async TCP/TLS socket and exposes a single linear event stream via `events()`.
The state machine internally:

1. Opens the TCP connection.
2. Sends the probe and reads the negotiated reply.
3. Upgrades to TLS if the negotiated features include `Encryption`. Fail-
   closed downgrade: if we offered TLS and the core declined it, abort BEFORE
   sending `ClientInit` so credentials never reach a plaintext socket. The
   only way past this is to construct the connection with `tls=False`.
4. Sends `ClientInit`, reads `ClientInitAck` (or rejection).
5. Sends `ClientLogin`, reads `ClientLoginAck` (`AuthError` on rejection).
6. Reads `SessionInit` and computes the negotiated feature set as the
   intersection of what we offered and what the core advertised.
7. Yields `SessionReady` (carrying the `SessionInit`) as the first event.
8. Enters the CONNECTED loop: read framed `SignalProxy` payloads, decode
   them, auto-reply to any `HeartBeat`, and yield a typed `ProtocolEvent`
   for each one.
9. On any error or peer disconnect, yields a final `Disconnected` event and
   stops the iterator.

Threading model: this module is single-task. There is no background reader,
no shared queue, no locks. The consumer of `events()` IS the read loop. If a
caller wants to write to the connection while iterating events, they need to
either (a) drive the iterator from one task and call `send()` from another
(both share the same writer, which is single-producer-friendly under the
asyncio event loop) or (b) wait for the next event before sending. Phase 5's
`QuasselClient` will own the task structure for option (a).

Heartbeat policy: a HeartBeat from the core is the *only* unsolicited
liveness check, and the core drops connections that don't reply within ~30s.
We send the reply *before* yielding the event so the timing isn't at the
mercy of how slowly the consumer iterates.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, NoReturn

from quasseltui.protocol.enums import (
    DEFAULT_CLIENT_FEATURES,
    LEGACY_EXTENDED_FEATURES,
    bitmask_to_features,
    features_to_bitmask,
)
from quasseltui.qt.datastream import QDataStreamError
from quasseltui.protocol.errors import (
    AuthError,
    HandshakeError,
    ProbeError,
    QuasselError,
)
from quasseltui.protocol.framing import read_frame, write_frame
from quasseltui.protocol.handshake import (
    encode_client_init,
    encode_client_login,
    recv_handshake_message,
)
from quasseltui.protocol.messages import (
    ClientInit,
    ClientInitAck,
    ClientInitReject,
    ClientLogin,
    ClientLoginAck,
    CoreSetupReject,
    SessionInit,
)
from quasseltui.protocol.probe import (
    ConnectionFeature,
    probe,
)
from quasseltui.protocol.signalproxy import (
    REQUEST_NAMES,
    HeartBeat,
    HeartBeatReply,
    InitData,
    InitRequest,
    RpcCall,
    SignalProxyError,
    SignalProxyMessage,
    SyncMessage,
    decode_signalproxy_payload,
    encode_signalproxy_payload,
)
from quasseltui.protocol.transport import (
    TlsOptions,
    close_writer,
    open_tcp_connection,
    start_tls_on_writer,
)

_log = logging.getLogger(__name__)


def _assert_never(value: Any) -> NoReturn:
    """Exhaustiveness helper for match-on-union code paths.

    Replaces `typing.assert_never` (3.11+) with a tiny local copy so mypy
    still narrows correctly in this file without relying on a newer stdlib
    export. Raises at runtime if somehow reached.
    """
    raise AssertionError(f"unexpected value {value!r} (unreachable)")


class ConnState(Enum):
    """Where the state machine currently is.

    `events()` advances through these monotonically — we never re-enter an
    earlier state once we've left it. Callers can read `connection.state`
    for diagnostics but should not act on it directly; the event stream is
    the supported observability surface.
    """

    INITIAL = "initial"
    PROBING = "probing"
    TLS_UPGRADING = "tls_upgrading"
    HANDSHAKE_INIT = "handshake_init"
    HANDSHAKE_LOGIN = "handshake_login"
    HANDSHAKE_SESSION = "handshake_session"
    CONNECTED = "connected"
    CLOSED = "closed"


# ---------------------------------------------------------------------------
# Public ProtocolEvent union. Names match the plan:
#   `SessionReady`, `SyncEvent`, `RpcEvent`, `InitDataEvent`,
#   `HeartBeatEvent`, `Disconnected`.
# Plus `InitRequestEvent` for the (rare, mostly client-to-core) case where
# the core asks us to provide initial state for an object.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionReady:
    """First event from `events()`. Hands the consumer the SessionInit map.

    `peer_features` is the negotiated feature set — the intersection of
    what we offered and what the core advertised in `ClientInitAck`. The
    Message decoder uses it to decide which conditional fields to read; the
    UI may eventually want it to know whether `realName` etc. are present.
    """

    session: SessionInit
    peer_features: frozenset[str]
    core_init_ack: ClientInitAck


@dataclass(frozen=True, slots=True)
class SyncEvent:
    """A `Sync` slot invocation from the core to one of our SyncObjects."""

    message: SyncMessage


@dataclass(frozen=True, slots=True)
class RpcEvent:
    """A top-level `RpcCall` (no SyncObject involved)."""

    message: RpcCall


@dataclass(frozen=True, slots=True)
class InitDataEvent:
    """A reply to an earlier `InitRequest` we sent."""

    message: InitData


@dataclass(frozen=True, slots=True)
class InitRequestEvent:
    """The core asked us to send `InitData` for `(class_name, object_name)`."""

    message: InitRequest


@dataclass(frozen=True, slots=True)
class HeartBeatEvent:
    """A core heartbeat. We've already replied by the time you see this."""

    message: HeartBeat


@dataclass(frozen=True, slots=True)
class Disconnected:
    """Terminal event. After this the iterator stops.

    `reason` is a short human-readable description (e.g. "auth rejected" or
    "core closed connection"). `error` is the raised exception that caused
    the close, if any — `None` for a clean shutdown initiated locally.
    """

    reason: str
    error: BaseException | None = field(default=None, compare=False)


ProtocolEvent = (
    SessionReady
    | SyncEvent
    | RpcEvent
    | InitDataEvent
    | InitRequestEvent
    | HeartBeatEvent
    | Disconnected
)


# ---------------------------------------------------------------------------
# QuasselConnection
# ---------------------------------------------------------------------------


_DEFAULT_BUILD_DATE = "1970-01-01"


class QuasselConnection:
    """One Quassel core connection: probe + handshake + signal stream."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        tls: bool = True,
        tls_options: TlsOptions | None = None,
        client_version: str = "quasseltui",
        build_date: str = _DEFAULT_BUILD_DATE,
        connect_timeout: float = 10.0,
        offered_features: tuple[str, ...] = DEFAULT_CLIENT_FEATURES,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._tls = tls
        self._tls_options = tls_options or TlsOptions()
        self._client_version = client_version
        self._build_date = build_date
        self._connect_timeout = connect_timeout
        self._offered_features = tuple(offered_features)

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._state: ConnState = ConnState.INITIAL
        self._peer_features: frozenset[str] = frozenset()
        self._session: SessionInit | None = None
        self._client_init_ack: ClientInitAck | None = None
        self._closed = False

    # -- public observability ------------------------------------------------

    @property
    def state(self) -> ConnState:
        return self._state

    @property
    def peer_features(self) -> frozenset[str]:
        return self._peer_features

    @property
    def session(self) -> SessionInit | None:
        return self._session

    # -- context-manager sugar ----------------------------------------------

    async def __aenter__(self) -> QuasselConnection:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    # -- main entry point ---------------------------------------------------

    async def events(self) -> AsyncIterator[ProtocolEvent]:
        """Drive the state machine and yield protocol events.

        Always yields exactly one terminal `Disconnected` event before the
        iterator stops, even on success — this gives consumers a single
        place to know the connection is gone. Re-iterating after that is
        a no-op (the connection is closed).
        """
        if self._state is not ConnState.INITIAL:
            raise RuntimeError(
                f"QuasselConnection.events() may only be called once (state is {self._state.name})"
            )

        try:
            ack = await self._do_handshake()
        except AuthError as exc:
            yield Disconnected(reason=f"auth rejected: {exc}", error=exc)
            await self._cleanup()
            return
        except QuasselError as exc:
            yield Disconnected(reason=f"handshake failed: {exc}", error=exc)
            await self._cleanup()
            return
        except (OSError, asyncio.IncompleteReadError) as exc:
            yield Disconnected(reason=f"transport error during handshake: {exc}", error=exc)
            await self._cleanup()
            return
        except Exception as exc:  # pragma: no cover - defensive catch-all
            yield Disconnected(reason=f"unexpected error during handshake: {exc}", error=exc)
            await self._cleanup()
            return

        assert self._session is not None  # set by _do_handshake on success
        self._client_init_ack = ack
        self._state = ConnState.CONNECTED
        yield SessionReady(
            session=self._session,
            peer_features=self._peer_features,
            core_init_ack=ack,
        )

        async for event in self._connected_loop():
            yield event

    # -- handshake driver ---------------------------------------------------

    async def _do_handshake(self) -> ClientInitAck:
        """Open + probe + TLS + ClientInit/Login/SessionInit. Returns the ack.

        On success, sets `self._session`, `self._peer_features`, and the
        reader/writer pair. On failure, raises one of the typed errors that
        `events()` catches.
        """
        self._state = ConnState.PROBING
        self._reader, self._writer = await open_tcp_connection(
            self._host,
            self._port,
            connect_timeout=self._connect_timeout,
        )
        offered_conn = ConnectionFeature.Encryption if self._tls else ConnectionFeature.NONE
        negotiated = await probe(
            self._reader,
            self._writer,
            offered_features=offered_conn,
        )

        if negotiated.tls_required:
            self._state = ConnState.TLS_UPGRADING
            await start_tls_on_writer(
                self._writer,
                host=self._host,
                options=self._tls_options,
            )
        elif self._tls:
            # Fail-closed: we offered Encryption and the core declined. Mirror
            # the CLI's `login-only` policy — never send credentials over a
            # plaintext socket unless the caller explicitly opted in.
            raise ProbeError(
                "core did not enable TLS but we offered it; refusing to send "
                "credentials over plaintext (construct with tls=False to override)"
            )

        # ---- ClientInit ----
        self._state = ConnState.HANDSHAKE_INIT
        init_msg = ClientInit(
            client_version=self._client_version,
            build_date=self._build_date,
            features=features_to_bitmask(self._offered_features),
            feature_list=self._offered_features,
        )
        await write_frame(self._writer, encode_client_init(init_msg))
        ack_msg = await recv_handshake_message(self._reader)
        if isinstance(ack_msg, ClientInitReject):
            raise HandshakeError(f"core rejected ClientInit: {ack_msg.error_string!r}")
        if isinstance(ack_msg, CoreSetupReject):
            raise HandshakeError(f"core setup rejected: {ack_msg.error_string!r}")
        if not isinstance(ack_msg, ClientInitAck):
            raise HandshakeError(
                f"unexpected handshake reply at init phase: {type(ack_msg).__name__}"
            )
        if not ack_msg.configured:
            raise HandshakeError("core is not configured (run quasselcore --setup first)")

        # AIDEV-NOTE: Quassel feature negotiation has three tiers:
        #
        # 1. Core returns a non-empty FeatureList → normal string
        #    intersection (modern cores).
        # 2. Core has ExtendedFeatures bit but empty FeatureList →
        #    the core processes our FeatureList and enables what it
        #    supports internally, but doesn't echo its own list back.
        #    We must assume all our offered features are active, or
        #    we'll misread the wire (e.g. int64 timestamps).
        # 3. Truly legacy core (no ExtendedFeatures) → only binary
        #    bitmask negotiation.
        string_features = frozenset(self._offered_features) & frozenset(ack_msg.feature_list)
        binary_features = frozenset(self._offered_features) & bitmask_to_features(
            ack_msg.core_features
        )
        if ack_msg.feature_list:
            # Tier 1: core advertises string features — trust the
            # intersection, supplemented by any binary-only features.
            self._peer_features = string_features | binary_features
        elif ack_msg.core_features & LEGACY_EXTENDED_FEATURES:
            # Tier 2: core understands string features but returned an
            # empty list — it will honour our FeatureList for any
            # feature it was compiled with. Assume all offered are
            # active (safe because we only offer what we can decode).
            self._peer_features = frozenset(self._offered_features)
        else:
            # Tier 3: purely binary negotiation.
            self._peer_features = binary_features

        # ---- ClientLogin ----
        self._state = ConnState.HANDSHAKE_LOGIN
        login_msg = ClientLogin(user=self._user, password=self._password)
        await write_frame(self._writer, encode_client_login(login_msg))
        login_ack = await recv_handshake_message(self._reader)
        # AuthError is raised inside parse_handshake_message; we shouldn't
        # see a ClientLoginReject here as a returned value.
        if not isinstance(login_ack, ClientLoginAck):
            raise HandshakeError(f"expected ClientLoginAck, got {type(login_ack).__name__}")

        # ---- SessionInit ----
        self._state = ConnState.HANDSHAKE_SESSION
        assert self._reader is not None
        session_msg = await recv_handshake_message(self._reader)
        if not isinstance(session_msg, SessionInit):
            raise HandshakeError(f"expected SessionInit, got {type(session_msg).__name__}")
        self._session = session_msg
        return ack_msg

    # -- CONNECTED loop -----------------------------------------------------

    async def _connected_loop(self) -> AsyncIterator[ProtocolEvent]:
        assert self._reader is not None
        assert self._writer is not None
        while True:
            try:
                payload = await read_frame(self._reader)
            except (OSError, asyncio.IncompleteReadError) as exc:
                yield Disconnected(reason=f"core closed connection: {exc}", error=exc)
                await self._cleanup()
                return
            except QuasselError as exc:
                yield Disconnected(reason=f"frame read error: {exc}", error=exc)
                await self._cleanup()
                return

            try:
                message = decode_signalproxy_payload(
                    payload,
                    peer_features=self._peer_features,
                )
            except SignalProxyError as exc:
                # Decode failures are unrecoverable — once we lose sync on
                # one frame the next frame's QVariantList header is
                # gibberish. Tear down and surface to the consumer.
                yield Disconnected(reason=f"signalproxy decode failed: {exc}", error=exc)
                await self._cleanup()
                return
            except (QuasselError, QDataStreamError) as exc:
                # QDataStreamError covers unsupported user types or
                # type IDs from older cores. Like SignalProxyError
                # these desynchronize the frame stream.
                yield Disconnected(reason=f"signalproxy decode failed: {exc}", error=exc)
                await self._cleanup()
                return

            # _handle_signalproxy may perform a write (HeartBeatReply).
            # A write failure there means the socket is dead — tear down
            # instead of swallowing the error and yielding a "healthy"
            # event to the consumer. This mirrors the frame-read error
            # handling above and satisfies the module's "on any error,
            # yield terminal Disconnected" contract.
            try:
                event = await self._handle_signalproxy(message)
            except (OSError, QuasselError) as exc:
                yield Disconnected(
                    reason=f"failed to process frame: {exc}",
                    error=exc,
                )
                await self._cleanup()
                return
            if event is not None:
                yield event

    async def _handle_signalproxy(
        self,
        message: SignalProxyMessage,
    ) -> ProtocolEvent | None:
        """Convert one decoded SignalProxy message into a ProtocolEvent.

        Heartbeat replies are sent here, BEFORE yielding the event, so the
        reply timing isn't gated on how fast the consumer iterates. Returns
        `None` for messages we consume internally (today: only HeartBeatReply,
        which we send and don't expect to receive).

        Write failures from the HeartBeat reply path are NOT caught here —
        they propagate to `_connected_loop` which converts them into a
        terminal `Disconnected` event. Swallowing a broken pipe here would
        mask a dead socket from the consumer (they'd see a healthy
        `HeartBeatEvent` while the connection was actually gone).
        """
        if isinstance(message, HeartBeat):
            await self._send_signalproxy(HeartBeatReply(timestamp=message.timestamp))
            return HeartBeatEvent(message=message)
        if isinstance(message, HeartBeatReply):
            # We never *send* HeartBeats from the client side, so a
            # HeartBeatReply from the core is unusual — log and drop.
            _log.debug("received unexpected HeartBeatReply ts=%s", message.timestamp)
            return None
        if isinstance(message, SyncMessage):
            return SyncEvent(message=message)
        if isinstance(message, RpcCall):
            return RpcEvent(message=message)
        if isinstance(message, InitData):
            return InitDataEvent(message=message)
        if isinstance(message, InitRequest):
            return InitRequestEvent(message=message)
        # Exhaustive over the SignalProxyMessage union — if the union ever
        # grows a new variant, mypy will flag missing handling here.
        _assert_never(message)

    # -- outbound -----------------------------------------------------------

    async def send(self, message: SignalProxyMessage) -> None:
        """Send one SignalProxy message to the core.

        Only valid in the CONNECTED state. The connection is single-writer
        from the asyncio perspective, so callers that share this object
        between tasks must ensure they don't interleave `send()` calls.
        """
        if self._state is not ConnState.CONNECTED:
            raise QuasselError(f"cannot send SignalProxy message in state {self._state.name}")
        await self._send_signalproxy(message)

    async def _send_signalproxy(self, message: SignalProxyMessage) -> None:
        assert self._writer is not None
        payload = encode_signalproxy_payload(message, peer_features=self._peer_features)
        await write_frame(self._writer, payload)

    # -- shutdown -----------------------------------------------------------

    async def close(self, *, reason: str = "client closed") -> None:
        if self._closed:
            return
        _log.debug("closing connection: %s", reason)
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._state = ConnState.CLOSED
        if self._writer is not None:
            await close_writer(self._writer)
        self._reader = None
        self._writer = None


__all__ = [
    "REQUEST_NAMES",
    "ConnState",
    "Disconnected",
    "HeartBeatEvent",
    "InitDataEvent",
    "InitRequestEvent",
    "ProtocolEvent",
    "QuasselConnection",
    "RpcEvent",
    "SessionReady",
    "SyncEvent",
]
