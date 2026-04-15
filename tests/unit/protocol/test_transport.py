"""Unit tests for `quasseltui.protocol.transport`.

The interesting surface here is `close_writer`: a best-effort helper
that has to be robust against a hostile / broken / hung peer. The most
important behavior is the TLS `wait_closed` hang guard — Python 3.11
will block indefinitely in `StreamWriter.wait_closed()` if the peer
doesn't send its own `close_notify` (cpython gh-88021, fixed in 3.12).
Every teardown path in quasseltui funnels through `close_writer`, so a
hang would leave Ctrl+Q stuck in the restored terminal with no
explanation. The fix caps the wait with `asyncio.wait_for` and aborts
the transport on timeout — we assert that contract here with a
hand-built writer stub so the test stays fast (no real TLS handshake)
and deterministic (no `asyncio.sleep` slippage).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from quasseltui.protocol.transport import CLOSE_WRITER_GRACE_SECONDS, close_writer


class _FakeTransport:
    """Record whether `abort()` was called by `close_writer`'s fallback.

    Stands in for `asyncio.BaseTransport` without pulling in the real
    event-loop machinery. `close_writer` only ever asks for `.transport`
    and calls `abort()` on it, so the surface can be very small.
    """

    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


class _HangingWriter:
    """Minimal `StreamWriter`-shaped object whose `wait_closed` hangs.

    Models the cpython gh-88021 deadlock: `close()` returns immediately
    (it just schedules `close_notify`), then `wait_closed()` awaits a
    future that is never resolved because the peer never replies.
    `close_writer` must time out and call `transport.abort()`.
    """

    def __init__(self) -> None:
        self.closed = False
        self.transport = _FakeTransport()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.Event().wait()  # never fires


class _FastWriter:
    """Cooperative writer that finishes `wait_closed` immediately.

    Used to pin the happy path: when the peer is well-behaved, we
    should NOT abort the transport — aborting a healthy socket would
    skip the TLS `close_notify` exchange unnecessarily.
    """

    def __init__(self) -> None:
        self.closed = False
        self.transport = _FakeTransport()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _RaisingWriter:
    """Writer whose `wait_closed` raises OSError (already-dead peer).

    `close_writer`'s contract is "best-effort on half-closed sockets":
    errors from a peer that already hung up must be swallowed, not
    re-raised into the caller's teardown path.
    """

    def __init__(self) -> None:
        self.transport = _FakeTransport()

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        raise OSError("peer already closed")


@pytest.mark.asyncio
async def test_close_writer_returns_quickly_on_happy_path() -> None:
    """A healthy peer must NOT trigger the abort fallback."""
    writer = _FastWriter()
    await close_writer(writer)  # type: ignore[arg-type]
    assert writer.closed is True
    assert writer.transport.aborted is False


@pytest.mark.asyncio
async def test_close_writer_aborts_on_tls_wait_closed_hang() -> None:
    """Regression for cpython gh-88021 TLS close hang.

    A TLS peer that never replies to our `close_notify` used to stall
    every teardown path in quasseltui forever — including Textual's
    `on_unmount` after Ctrl+Q, leaving the user staring at a dead
    restored terminal. `close_writer` must now bound the wait with
    `CLOSE_WRITER_GRACE_SECONDS` and fall through to `transport.abort()`
    so the process can exit.

    We cheat the clock by monkey-patching `asyncio.wait_for`'s timeout
    via a very short grace window isn't enough — the real `wait_for`
    will still create a timer task. Instead we pass a writer that
    hangs forever and assert `close_writer` returns within a bound that
    covers `CLOSE_WRITER_GRACE_SECONDS` plus a generous scheduler slack.
    """
    writer = _HangingWriter()
    started = asyncio.get_running_loop().time()
    await close_writer(writer)  # type: ignore[arg-type]
    elapsed = asyncio.get_running_loop().time() - started
    # The close_writer contract is "return within grace + a little
    # scheduling slop". Two seconds of grace + one second of slack is
    # plenty; on CI we observed <2.1s in practice.
    assert elapsed < CLOSE_WRITER_GRACE_SECONDS + 1.0
    assert writer.closed is True
    assert writer.transport.aborted is True


@pytest.mark.asyncio
async def test_close_writer_swallows_oserror_from_dead_peer() -> None:
    """Half-closed peers must not propagate their errors upward.

    If the remote end has already hung up, `wait_closed` may raise
    `OSError` / `ssl.SSLError`. The teardown path never wants to know
    — it just wants the socket gone — so `close_writer` swallows
    these quietly and does NOT abort (the transport is already dying).
    """
    writer = _RaisingWriter()
    await close_writer(writer)  # type: ignore[arg-type]
    assert writer.transport.aborted is False


@pytest.mark.asyncio
async def test_close_writer_handles_none_transport_on_timeout() -> None:
    """Defensive branch: a writer whose `transport` attribute is `None`.

    Not a shape we produce, but asyncio's `StreamWriter.transport` is
    typed `BaseTransport | None` and the close_writer fallback has a
    `None` guard. Pin it so a future refactor doesn't reintroduce the
    `AttributeError: 'NoneType' object has no attribute 'abort'` bug.
    """

    class _NoTransportWriter:
        def __init__(self) -> None:
            self.transport: Any = None

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            await asyncio.Event().wait()

    writer = _NoTransportWriter()
    await close_writer(writer)  # type: ignore[arg-type]
    # No crash is the assertion; nothing else to check.
