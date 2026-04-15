"""End-to-end test: QuasselApp + ClientBridge + widgets.

Complements the pure-bridge tests in `test_bridge.py` by mounting a
real `QuasselApp` via Textual's `run_test` pilot and feeding it events
through a stub client. The goal is to catch regressions where the
bridge posts the right messages but the app's handlers, widget
methods, or `query_one` plumbing has drifted — phase 6 has precedent
for that kind of bug (the codex review caught Tree.process_label
markup reinterpretation).

The stub client is the minimum surface `QuasselApp` uses:
`state`, `events()`, `close()`. We do NOT instantiate a real
`QuasselClient` because its `__init__` demands host/port/credentials
and opens a socket. The stub matches the `Protocol`-like shape
structurally and is passed as the `client` kwarg; mypy is appeased
with a `# type: ignore[arg-type]` on the call site because
`QuasselApp` types that arg as the concrete `QuasselClient`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator

import pytest

from quasseltui.app.app import QuasselApp
from quasseltui.app.widgets.buffer_tree import BufferTree
from quasseltui.app.widgets.message_log import MessageLog
from quasseltui.client.state import ClientState
from quasseltui.protocol.enums import MessageFlag, MessageType
from quasseltui.protocol.messages import SessionInit
from quasseltui.protocol.usertypes import (
    BufferId,
    BufferInfo,
    BufferType,
    MsgId,
    NetworkId,
)
from quasseltui.sync.events import (
    BufferAdded,
    ClientDisconnected,
    ClientEvent,
    IrcMessage,
    MessageReceived,
    SessionOpened,
)
from quasseltui.sync.network import Network, NetworkConnectionState


class _StubClient:
    """Smallest surface `QuasselApp` needs from a client.

    Holds a pre-built event queue that the bridge will drain in
    order, then blocks indefinitely so the bridge doesn't emit a
    terminal `ClientDisconnected` during the test. Exposes a
    `push_event` helper the test uses to feed events mid-flight
    (simulating live traffic while the pilot is running).

    `close()` sets an internal flag so tests can verify the app's
    `on_unmount` reached its cleanup branch.
    """

    def __init__(self, state: ClientState) -> None:
        self.state = state
        self._queue: asyncio.Queue[ClientEvent] = asyncio.Queue()
        self.closed = False

    def push_event(self, event: ClientEvent) -> None:
        self._queue.put_nowait(event)

    async def events(self) -> AsyncIterator[ClientEvent]:
        while True:
            yield await self._queue.get()

    async def close(self) -> None:
        self.closed = True


def _empty_state_with_one_network() -> ClientState:
    state = ClientState()
    network = Network("1")
    network.network_name = "Libera.Chat"
    network.my_nick = "seanr"
    network.connection_state = NetworkConnectionState.Initialized
    state.networks[NetworkId(1)] = network
    return state


def _buffer(bid: int, name: str = "#python") -> BufferInfo:
    return BufferInfo(
        buffer_id=BufferId(bid),
        network_id=NetworkId(1),
        type=BufferType.Channel,
        group_id=0,
        name=name,
    )


def _irc_message(bid: int, *, msg_id: int, contents: str = "hi") -> IrcMessage:
    return IrcMessage(
        msg_id=MsgId(msg_id),
        buffer_id=BufferId(bid),
        network_id=NetworkId(1),
        timestamp=dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.UTC),
        type=MessageType.Plain,
        flags=MessageFlag.NONE,
        sender="alice",
        sender_prefixes="",
        contents=contents,
    )


@pytest.mark.asyncio
async def test_buffer_added_event_refreshes_sidebar() -> None:
    """Live `BufferAdded` must show up in the `BufferTree` on next tick.

    We start the app with an empty state (one network, no buffers), then
    push a `BufferAdded` event into the stub client's queue. The pilot
    pauses to let the bridge drain, the app's handler fires, and the
    tree's `refresh_from_state()` rebuilds with the new buffer.
    """
    state = _empty_state_with_one_network()
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        # Initial paint: one network, zero buffers.
        tree = app.screen.query_one(BufferTree)
        assert len(tree.root.children) == 1
        assert len(tree.root.children[0].children) == 0

        # Simulate the dispatcher: mutate state, then emit the event.
        # The bridge does not touch state itself — it only reflects it.
        buf = _buffer(11, name="#python")
        state.buffers[buf.buffer_id] = buf
        state.messages[buf.buffer_id] = []
        client.push_event(
            BufferAdded(
                buffer_id=buf.buffer_id,
                network_id=buf.network_id,
                name=buf.name,
                type=buf.type,
            )
        )
        await pilot.pause()
        await pilot.pause()

        tree = app.screen.query_one(BufferTree)
        assert len(tree.root.children[0].children) == 1
        leaf_labels = [c.label.plain for c in tree.root.children[0].children]
        assert "#python" in leaf_labels


@pytest.mark.asyncio
async def test_message_received_for_default_active_buffer_shows_in_log() -> None:
    """A `MessageReceived` after default-pick renders in the message log.

    This exercises the bridge's "pick a default active buffer on the
    first event that mentions one, then debounce-refresh the log"
    flow end-to-end. We seed the state with a buffer that already
    has content so default-pick lands on it immediately, push a new
    message, and assert the log has at least the seeded line plus
    the pushed one after the debounce window closes.
    """
    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1, contents="welcome")]

    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        # The bridge hasn't run yet because we've pushed no event.
        # Default-pick happens on the first event, so we send a
        # BufferAdded to trigger it (state already has the buffer, so
        # this is just a signal).
        client.push_event(
            BufferAdded(
                buffer_id=buf.buffer_id,
                network_id=buf.network_id,
                name=buf.name,
                type=buf.type,
            )
        )
        await pilot.pause()
        await pilot.pause()

        # Now push a live message for the active buffer.
        state.messages[buf.buffer_id].append(_irc_message(11, msg_id=2, contents="glad to be here"))
        client.push_event(
            MessageReceived(message=_irc_message(11, msg_id=2, contents="glad to be here"))
        )
        # Let the debounce window close (50ms default) and the next
        # pilot tick redraw the log.
        await pilot.pause(0.1)

        log = app.screen.query_one(MessageLog)
        rendered = " ".join(strip.text for strip in log.lines)
        assert "welcome" in rendered
        assert "glad to be here" in rendered


@pytest.mark.asyncio
async def test_client_is_closed_on_unmount() -> None:
    """Quitting the app must call `client.close()` exactly once.

    Without this, a `ui` session that ends cleanly would still leak
    the socket until GC collected the `QuasselClient`. The app
    lifecycle is the right place to enforce the cleanup; the bridge
    worker doesn't know anything about the client's close method.
    """
    state = _empty_state_with_one_network()
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()

    assert client.closed is True


@pytest.mark.asyncio
async def test_early_disconnect_before_session_opened_exits_fatally() -> None:
    """Regression for codex review finding: startup handshake/auth
    failures must be user-visible, not silently swallowed.

    Before the fix, `_on_session_ended` only called `_log.warning`,
    which Textual's alternate-screen mode hides. A failed handshake
    would leave the user staring at a blank chat screen with no
    explanation and the process still exiting cleanly on Ctrl+Q.
    The fix is to track whether `SessionStarted` has fired and exit
    fatally on any `SessionEnded` that arrives first.
    """
    state = _empty_state_with_one_network()
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        # Push a disconnect before any SessionStarted — the handshake
        # effectively failed (e.g., auth rejected, TLS handshake
        # error, core sent ClientInitReject).
        client.push_event(ClientDisconnected(reason="auth rejected", error=None))
        # The app calls `self.exit(return_code=1, ...)` from inside
        # the handler; the pilot cooperates with the teardown so
        # exiting the `async with` is enough.
        await pilot.pause()
        await pilot.pause()

    assert app.return_code == 1


@pytest.mark.asyncio
async def test_disconnect_after_session_opened_does_not_exit_fatally() -> None:
    """Mid-session drops are non-fatal — the user keeps the last state.

    Complements `test_early_disconnect_before_session_opened_exits_fatally`
    by pinning the other branch: once a `SessionStarted` has fired,
    a later `SessionEnded` must NOT auto-exit the app. Phase 11 will
    surface it in a status bar with an optional reconnect supervisor.
    """
    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = []
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        # First, simulate a successful handshake.
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        # Then disconnect mid-session.
        client.push_event(ClientDisconnected(reason="connection lost", error=None))
        await pilot.pause()
        await pilot.pause()
        # We should still be able to quit cleanly via Ctrl+Q —
        # verifying that the app did NOT auto-exit on the disconnect.
        await pilot.press("ctrl+q")
        await pilot.pause()

    assert app.return_code in (None, 0)


@pytest.mark.asyncio
async def test_fatal_exit_message_is_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression for codex review finding: disconnect reasons can
    carry core-supplied text (e.g. `ClientLoginReject.error_string`)
    which is untrusted and may contain ANSI / C0 / C1 control bytes.
    Those must not reach the terminal verbatim — the handler must
    sanitize before logging and before handing the string to
    `App.exit(message=...)`.

    We assert via `caplog` rather than `_exit_renderables` because
    Textual drains the exit renderables during teardown, so by the
    time the `async with` unwinds the list is empty. The warning
    log line still carries the sanitized form and is captured by
    pytest's caplog fixture.
    """
    import logging

    state = _empty_state_with_one_network()
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="quasseltui.app.app"):
        async with app.run_test() as pilot:
            await pilot.pause()
            client.push_event(ClientDisconnected(reason="\x1b[31mREJECTED\x07", error=None))
            await pilot.pause()
            await pilot.pause()

    logged = [r.getMessage() for r in caplog.records if "REJECTED" in r.getMessage()]
    assert logged, "handler did not log the disconnect"
    for msg in logged:
        assert "\x1b" not in msg
        assert "\x07" not in msg
        assert "\\x1b" in msg
    # The app also exited with return_code=1 — that's the other half
    # of the user-visible-failure contract. Without the exit the
    # user would stay in a blank Textual screen.
    assert app.return_code == 1
