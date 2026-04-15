"""Async TCP + TLS transport for connecting to a Quassel core.

The probe handshake happens on a plain TCP socket; if the negotiated
features include `Encryption` we then upgrade the same socket to TLS by
calling `StreamWriter.start_tls`. Both halves of the existing
reader/writer pair stay valid across the upgrade — that's the whole point
of the in-place upgrade and is the only way the Quassel handshake works,
since the core does not allow reconnecting on a fresh socket after the
probe.

`open_tcp_connection` is a thin wrapper around `asyncio.open_connection`
with a connect timeout and a typed error class. `start_tls_on_writer` is
a thin wrapper around `StreamWriter.start_tls` that builds the SSL context
the user asked for. Neither knows anything about Quassel — they're just
the I/O primitives the probe and connection state machine compose with.
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass

from quasseltui.protocol.errors import QuasselError

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0


class TransportError(QuasselError):
    """Connect, TLS upgrade, or low-level I/O failure."""


@dataclass(frozen=True, slots=True)
class TlsOptions:
    """How to build the SSL context for the post-probe TLS upgrade.

    `verify` toggles certificate verification — turn it off only for
    self-signed cores you actually trust; the user has to opt in via an
    explicit `--insecure` flag at the CLI level. `cafile` and `capath`
    point at additional trust anchors. `server_hostname` overrides the
    SNI/verify hostname when it differs from the connect host (rare but
    needed for cores fronted by a proxy with a different cert CN).
    """

    verify: bool = True
    cafile: str | None = None
    capath: str | None = None
    server_hostname: str | None = None

    def build_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=self.cafile, capath=self.capath)
        if not self.verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx


async def open_tcp_connection(
    host: str,
    port: int,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a plain TCP connection with a bounded connect timeout."""
    try:
        return await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=connect_timeout,
        )
    except TimeoutError as exc:
        raise TransportError(
            f"timed out connecting to {host}:{port} after {connect_timeout}s"
        ) from exc
    except OSError as exc:
        raise TransportError(f"failed to connect to {host}:{port}: {exc}") from exc


async def start_tls_on_writer(
    writer: asyncio.StreamWriter,
    *,
    host: str,
    options: TlsOptions,
) -> None:
    """Upgrade an open writer (and its paired reader) to TLS in place.

    Must be called immediately after the probe reply if the negotiated
    features include `Encryption`, with no intervening reads or writes —
    the core expects the very next bytes on this socket to be the TLS
    ClientHello. The paired StreamReader stays valid; asyncio rewires its
    transport to the TLS one under the hood.
    """
    ctx = options.build_context()
    server_hostname = options.server_hostname or host
    try:
        await writer.start_tls(ctx, server_hostname=server_hostname)
    except (ssl.SSLError, OSError) as exc:
        raise TransportError(f"TLS upgrade to {host} failed: {exc}") from exc


CLOSE_WRITER_GRACE_SECONDS = 2.0
"""Upper bound on how long we wait for a graceful TLS close_notify reply.

`StreamWriter.wait_closed()` on a TLS transport waits for the peer to
reply with its own `close_notify` alert. If the peer never bothers
(broken core, already-closed socket, network partition) this blocks
forever on Python 3.11 — see cpython gh-88021, which was only fixed in
3.12. We can't upgrade the target runtime unilaterally, so we cap the
grace window here and fall back to `transport.abort()` on timeout so
process teardown is snappy. Two seconds is generous for any core that
is actually going to reply; anything longer would delay Ctrl+Q in the
TUI noticeably."""


async def close_writer(writer: asyncio.StreamWriter) -> None:
    """Close a writer cleanly, swallowing errors and bounding the wait.

    The protocol layer often wants to tear down the connection in error
    paths where the socket may already be half-closed. We don't want a
    secondary `ConnectionResetError` to mask the original failure, so
    errors are logged nothing / return nothing — the close is
    best-effort.

    Additionally, `StreamWriter.wait_closed()` on a TLS transport is
    prone to hanging in Python 3.11 when the peer does not reply to our
    `close_notify` — the known gh-88021 deadlock, fixed in 3.12. Since
    this helper runs on every teardown path (including the TUI's
    `on_unmount` → `QuasselClient.close` chain on Ctrl+Q), a hang here
    would leave the process stuck in a restored terminal with no
    explanation. Bound the wait with `asyncio.wait_for` and fall back
    to `transport.abort()` — which synchronously forces the socket
    closed without waiting for the peer's `close_notify` — if the
    graceful close doesn't land in `CLOSE_WRITER_GRACE_SECONDS`.
    """
    try:
        writer.close()
    except (OSError, ssl.SSLError):
        return
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=CLOSE_WRITER_GRACE_SECONDS)
    except TimeoutError:
        transport = writer.transport
        if transport is not None:
            transport.abort()
    except (OSError, ssl.SSLError):
        pass
