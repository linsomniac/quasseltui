"""`QuasselClient` — the embeddable library surface.

This is the one public class embedders (including the Textual UI in phase
6+) interact with. It wraps a `QuasselConnection`, owns a `ClientState` and
a `Dispatcher`, and exposes `events()` — an async iterator that yields
`ClientEvent` dataclasses.

Responsibilities:

1. Drive the protocol state machine via the wrapped connection.
2. Translate each `ProtocolEvent` into the dispatcher mutation that
   corresponds to it, then forward any `ClientEvent`s the dispatcher
   emitted.
3. After `SessionReady`, fan out `InitRequest` messages for the global
   singletons (`BufferSyncer`) and each per-network SyncObject. The core
   won't emit most `InitData` spontaneously — we have to ask for it.

Concurrency model: `QuasselClient` is single-task. `events()` is a normal
async generator and drives the protocol read loop inline. Outbound writes
(`send_input` in phase 9) are safe to call from another task because the
underlying `StreamWriter` is single-writer from the asyncio side and the
client itself doesn't currently write from the receive loop; when it does
(InitRequest fan-out), it's strictly sequenced by awaiting before yielding.

For phase 5 we don't expose `send_input` yet — the plan delays outbound
user messages to phase 9. We expose a private `_send_init_request` used by
the fan-out.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from quasseltui.client.state import ClientState
from quasseltui.protocol.connection import (
    Disconnected as ProtoDisconnected,
)
from quasseltui.protocol.connection import (
    HeartBeatEvent,
    InitDataEvent,
    InitRequestEvent,
    ProtocolEvent,
    QuasselConnection,
    RpcEvent,
    SessionReady,
    SyncEvent,
)
from quasseltui.protocol.enums import DEFAULT_CLIENT_FEATURES
from quasseltui.protocol.errors import QuasselError
from quasseltui.protocol.signalproxy import InitRequest
from quasseltui.protocol.transport import TlsOptions
from quasseltui.sync.buffer_syncer import BufferSyncer
from quasseltui.sync.dispatcher import Dispatcher
from quasseltui.sync.events import ClientDisconnected, ClientEvent
from quasseltui.sync.network import Network


class QuasselClient:
    """Embeddable Quassel client wrapping one `QuasselConnection`."""

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
        build_date: str = "1970-01-01",
        connect_timeout: float = 10.0,
        offered_features: tuple[str, ...] = DEFAULT_CLIENT_FEATURES,
        max_messages_per_buffer: int = 5000,
    ) -> None:
        self._connection = QuasselConnection(
            host=host,
            port=port,
            user=user,
            password=password,
            tls=tls,
            tls_options=tls_options,
            client_version=client_version,
            build_date=build_date,
            connect_timeout=connect_timeout,
            offered_features=offered_features,
        )
        self.state = ClientState(max_messages_per_buffer=max_messages_per_buffer)
        self._pending_events: list[ClientEvent] = []
        self._dispatcher = Dispatcher(state=self.state, emit=self._pending_events.append)
        self._closed = False

    # -- public API ----------------------------------------------------------

    @property
    def peer_features(self) -> frozenset[str]:
        return self.state.peer_features

    @property
    def connection(self) -> QuasselConnection:
        """Read-only access to the underlying connection for diagnostics."""
        return self._connection

    async def events(self) -> AsyncIterator[ClientEvent]:
        """Drive the protocol connection and yield client-facing events.

        Always yields exactly one terminal `ClientDisconnected` before
        stopping. Re-iterating after that is a no-op — the underlying
        connection is closed. Errors raised by the dispatcher, by the
        `SessionReady` fan-out (InitRequest writes), or by the enclosing
        protocol loop are converted into a terminal `ClientDisconnected`
        so the caller never sees an uncaught exception leak out of the
        async iterator.
        """
        async for proto_event in self._connection.events():
            try:
                self._handle_protocol_event(proto_event)
            except (OSError, QuasselError) as exc:
                yield ClientDisconnected(
                    reason=f"dispatcher error: {exc}",
                    error=exc,
                )
                await self._connection.close()
                return
            # Drain any events the dispatcher (and session fan-out below)
            # appended to our buffer.
            while self._pending_events:
                yield self._pending_events.pop(0)
            # Session-ready fan-out has to happen after we've yielded the
            # SessionOpened event so a caller observing a specific order
            # sees (SessionOpened, *NetworkAdded, ..., *InitData effects).
            # Fan-out sends InitRequests; the InitData responses come back
            # via `proto_event = InitDataEvent` on subsequent iterations.
            if isinstance(proto_event, SessionReady):
                try:
                    await self._fanout_init_requests()
                except (OSError, QuasselError) as exc:
                    # The underlying connection.events() loop only catches
                    # exceptions raised inside its read loop — writes made
                    # from this side (our InitRequest fan-out) need their
                    # own terminal-conversion or the async iterator leaks
                    # the exception and breaks the "always yields a
                    # terminal Disconnected" contract.
                    yield ClientDisconnected(
                        reason=f"init request fan-out failed: {exc}",
                        error=exc,
                    )
                    await self._connection.close()
                    return
                while self._pending_events:
                    yield self._pending_events.pop(0)
            if isinstance(proto_event, ProtoDisconnected):
                return

    async def close(self) -> None:
        """Idempotent shutdown. Safe to call in a ``finally`` block."""
        if self._closed:
            return
        self._closed = True
        await self._connection.close()

    # -- internal dispatch ---------------------------------------------------

    def _handle_protocol_event(self, event: ProtocolEvent) -> None:
        if isinstance(event, SessionReady):
            self._dispatcher.seed_from_session(event.session, event.peer_features)
            return
        if isinstance(event, SyncEvent):
            self._dispatcher.handle_sync(event.message)
            return
        if isinstance(event, InitDataEvent):
            self._dispatcher.handle_init_data(event.message)
            return
        if isinstance(event, RpcEvent):
            self._dispatcher.handle_rpc(event.message)
            return
        if isinstance(event, InitRequestEvent):
            # The core rarely asks us for init data — we don't host any
            # SyncObjects on the client side in v1, so anything it requests
            # is something we can't provide. Log-and-drop via the default
            # dispatcher debug log; no public event is emitted.
            return
        if isinstance(event, HeartBeatEvent):
            # Connection already replied — nothing more to do here.
            return
        if isinstance(event, ProtoDisconnected):
            self._pending_events.append(ClientDisconnected(reason=event.reason, error=event.error))
            return

    async def _fanout_init_requests(self) -> None:
        """Ask the core for InitData for every global + per-network singleton.

        Quassel's core won't volunteer most initial state — it waits for
        the client to InitRequest each object by class+object name. We
        fire off one request per:

        - `BufferSyncer("")`
        - `Network(<netId>)` for every NetworkId in SessionInit

        Identities are already fully populated from the session's raw
        identity list, and `IrcUser` / `IrcChannel` are created as a side
        effect of expanding `Network.IrcUsersAndChannels`, so neither
        needs its own InitRequest.

        Failures here raise through to `events()` and become a terminal
        `ClientDisconnected` — if we can't send an InitRequest something
        is seriously wrong with the socket.
        """
        await self._send_init_request(BufferSyncer.CLASS_NAME, "")
        if self.state.session is None:
            return
        for nid in self.state.session.network_ids:
            await self._send_init_request(Network.CLASS_NAME, str(int(nid)))

    async def _send_init_request(self, class_name: bytes, object_name: str) -> None:
        await self._connection.send(InitRequest(class_name=class_name, object_name=object_name))

    # -- context manager -----------------------------------------------------

    async def __aenter__(self) -> QuasselClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()


__all__ = [
    "QuasselClient",
]
