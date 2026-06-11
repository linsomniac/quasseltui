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
from quasseltui.app.messages import BufferSelected
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
    BufferRemoved,
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
        self.last_seen_calls: list[tuple[BufferId, MsgId]] = []
        self.marker_calls: list[tuple[BufferId, MsgId]] = []

    def push_event(self, event: ClientEvent) -> None:
        self._queue.put_nowait(event)

    async def events(self) -> AsyncIterator[ClientEvent]:
        while True:
            yield await self._queue.get()

    async def request_backlog(
        self, buffer_id: BufferId, limit: int = 100
    ) -> None:  # pragma: no cover - stub
        pass

    async def set_last_seen(self, buffer_id: BufferId, msg_id: MsgId) -> None:
        self.last_seen_calls.append((buffer_id, msg_id))

    async def set_marker_line(self, buffer_id: BufferId, msg_id: MsgId) -> None:
        self.marker_calls.append((buffer_id, msg_id))

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


def _option_text(log: MessageLog, index: int) -> str:
    """Read the plain text of a `MessageLog` option at `index`.

    Used by assertions that want to see "what did the user actually
    get rendered in this row". `Option.prompt` is a Rich `Text` for
    every row we build, so we lean on `.plain`; fall back to `str(...)`
    defensively in case a future row kind uses a bare string.
    """
    prompt = log.get_option_at_index(index).prompt
    return prompt.plain if hasattr(prompt, "plain") else str(prompt)


def _irc_message(bid: int, *, msg_id: int, contents: str = "hi") -> IrcMessage:
    # "Now" rather than a fixed date: the message log inserts disabled
    # date-separator rows before non-today messages, and most tests here
    # assert option ids/indices for plain "today" traffic. Tests that
    # exercise the separators override the timestamp explicitly.
    return IrcMessage(
        msg_id=MsgId(msg_id),
        buffer_id=BufferId(bid),
        network_id=NetworkId(1),
        timestamp=dt.datetime.now(dt.UTC),
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
        rendered = " ".join(_option_text(log, i) for i in range(log.option_count))
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
    The fix has the bridge stamp `SessionEnded.fatal=True` on any
    disconnect that arrives before a successful `SessionOpened`, and
    the app exits with return code 1 when it sees that flag.
    """
    state = _empty_state_with_one_network()
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        # Push a disconnect before any SessionOpened — the handshake
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
    by pinning the other branch: once `SessionOpened` has fired, the
    bridge stamps `SessionEnded.fatal=False` on any subsequent
    disconnect, and the app does NOT auto-exit. Phase 11 will surface
    it in a status bar with an optional reconnect supervisor.
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
async def test_mid_session_disconnect_is_visible_and_disables_input() -> None:
    """Regression for the 2026-06 review's top "feels flaky" finding.

    A mid-session drop used to only `_log.warning`, which the alternate
    screen hides — the UI went quiet with no explanation and the input
    bar kept silently swallowing lines. Now the app records the loss in
    `connection_lost`, disables the input bar with a placeholder that
    names the reason, and pushes a user notice, so the user can SEE that
    the connection is gone instead of typing into the void.
    """
    from quasseltui.app.widgets.input_bar import InputBar

    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = []
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()

        input_bar = app.screen.query_one(InputBar)
        assert app.connection_lost is False
        assert input_bar.disabled is False

        client.push_event(ClientDisconnected(reason="core went away", error=None))
        await pilot.pause()
        await pilot.pause()

        assert app.connection_lost is True
        assert input_bar.disabled is True
        assert "core went away" in input_bar.placeholder
        assert any("core went away" in notice for notice in app.notices)
        # Mid-session drop must NOT exit the app — the user keeps their
        # scrollback and quits on their own terms.
        assert app.return_code in (None, 0)


@pytest.mark.asyncio
async def test_send_failure_notifies_user() -> None:
    """A failed send must tell the user, not just silently bounce back.

    Complements `test_input_text_survives_send_failure`: that pins the
    text-restore; this pins the *feedback*. Without a notice, a racey
    send failure looks exactly like "I pressed Enter and nothing
    happened" — a direct contributor to the flaky feeling.
    """
    from quasseltui.app.widgets.input_bar import InputBar
    from quasseltui.protocol.errors import QuasselError

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1)]

    class _FailingSendClient(_StubClient):
        async def send_input(
            self, buffer_id: BufferId, text: str
        ) -> None:  # pragma: no cover - trivial
            raise QuasselError("simulated broken pipe")

    client = _FailingSendClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        input_bar = app.screen.query_one(InputBar)
        input_bar.value = "retry me"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert input_bar.value == "retry me"
        assert any("not sent" in notice.lower() for notice in app.notices)


@pytest.mark.asyncio
async def test_fatal_exit_reason_is_truncated_if_long(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression for codex review finding: a hostile / badly-behaving
    core can return an arbitrarily long disconnect reason. Without a
    cap, that would dump into stderr and the Textual exit banner.
    After `_sanitize_and_truncate_reason` we cap at 400 characters
    with an explicit `...[truncated]` marker.
    """
    import logging

    state = _empty_state_with_one_network()
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    long_reason = "A" * 5000
    with caplog.at_level(logging.WARNING, logger="quasseltui.app.app"):
        async with app.run_test() as pilot:
            await pilot.pause()
            client.push_event(ClientDisconnected(reason=long_reason, error=None))
            await pilot.pause()
            await pilot.pause()

    logged = [r.getMessage() for r in caplog.records if "session ended" in r.getMessage()]
    assert logged, "handler did not log the disconnect"
    for msg in logged:
        # The logged line is `session ended: <reason>`. The reason
        # portion must be shorter than the original 5000 and end
        # with the truncation marker.
        reason_part = msg.split("session ended: ", 1)[1]
        assert len(reason_part) < len(long_reason)
        assert reason_part.endswith("...[truncated]")
    assert app.return_code == 1


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


# ---------------------------------------------------------------------------
# Scroll anchoring (2026-06 review items 3 & 4)
# ---------------------------------------------------------------------------


def _lines(bid: int, ids: range) -> list[IrcMessage]:
    """Build a contiguous run of single-line messages for scroll tests."""
    return [_irc_message(bid, msg_id=i, contents=f"line {i}") for i in ids]


def _top_visible_id(log: MessageLog) -> str | None:
    """The option id genuinely at the top of the viewport.

    Goes through Textual's line cache (`_lines` maps each rendered scroll
    line to its option index) rather than assuming one line per option —
    long IRC lines wrap, so `round(scroll_y)` is NOT the option index.
    """
    lines = log._lines
    if not lines:
        return None
    y = max(0, min(len(lines) - 1, round(log.scroll_y)))
    return log.get_option_at_index(lines[y][0]).id


async def _open_buffer(pilot: object, app: QuasselApp, client: _StubClient) -> MessageLog:
    await pilot.pause()  # type: ignore[attr-defined]
    session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
    client.push_event(SessionOpened(session=session, peer_features=frozenset()))
    await pilot.pause()  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    return app.screen.query_one(MessageLog)


@pytest.mark.asyncio
async def test_live_message_keeps_scrolled_up_reader_anchored() -> None:
    """A live message must not yank a scrolled-up reader to the top.

    The dominant "feels flaky" symptom: reading history on a busy
    channel while new lines arrive, the ~50ms refresh rebuilds the log
    and `clear_options` resets the scroll to the top every time. The fix
    anchors the viewport on the message that was at the top so an append
    below the fold leaves the reader exactly where they were.
    """
    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = _lines(11, range(1, 101))
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 24)) as pilot:
        log = await _open_buffer(pilot, app, client)
        assert app.active_buffer_id == buf.buffer_id

        log.scroll_to(y=20, animate=False, immediate=True)
        await pilot.pause()
        assert not log.is_vertical_scroll_end
        top_before = _top_visible_id(log)

        # A live message arrives for the active buffer — same refresh
        # path the bridge's debounced ActiveBufferUpdated drives.
        state.messages[buf.buffer_id].append(_irc_message(11, msg_id=101, contents="newest"))
        log.set_active_buffer(buf.buffer_id)
        await pilot.pause()

        assert _top_visible_id(log) == top_before
        assert not log.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_backlog_prepend_keeps_reader_anchored() -> None:
    """Backlog merge prepends older rows; the reader must stay on content.

    Opening a channel fires a backlog request whose reply inserts N
    older messages ABOVE the current view. Resetting scroll to the top
    dumps the reader into ancient history. The same top-anchor keeps the
    previously-top message in place, with the fetched history added above
    it.
    """
    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = _lines(11, range(51, 101))
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 24)) as pilot:
        log = await _open_buffer(pilot, app, client)

        log.scroll_to(y=5, animate=False, immediate=True)
        await pilot.pause()
        assert not log.is_vertical_scroll_end
        top_before = _top_visible_id(log)

        older = [_irc_message(11, msg_id=i, contents=f"old {i}") for i in range(1, 51)]
        state.messages[buf.buffer_id] = older + state.messages[buf.buffer_id]
        log.set_active_buffer(buf.buffer_id)
        await pilot.pause()

        assert _top_visible_id(log) == top_before


@pytest.mark.asyncio
async def test_focus_while_scrolled_up_does_not_jump_to_tail() -> None:
    """Tabbing into the log while scrolled up must not snap to newest.

    `on_focus` used to always highlight the last message, which scrolls
    to the tail and rips the reader away from the history they were
    looking at. When scrolled up we instead land the cursor on the
    top-visible message — still selectable for a marker, but no jump.
    """
    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = _lines(11, range(1, 101))
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 24)) as pilot:
        log = await _open_buffer(pilot, app, client)

        log.scroll_to(y=20, animate=False, immediate=True)
        await pilot.pause()
        assert not log.is_vertical_scroll_end

        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.focused is log

        # The view stayed put (no snap to the bottom) and a visible row
        # is highlighted so Enter still places a marker.
        assert not log.is_vertical_scroll_end
        assert log.highlighted is not None
        assert log.highlighted < 40


@pytest.mark.asyncio
async def test_wrapped_backlog_prepend_keeps_reader_anchored() -> None:
    """Codex finding 1: the anchor must survive wrapped multi-line rows.

    Long IRC lines wrap, so one option spans several rendered scroll
    lines and `round(scroll_y)` is not the option index. The first
    anchor implementation assumed one line per option and restored to the
    wrong content after a backlog prepend. The line-cache anchor keeps
    the reader on the same message even when rows wrap.
    """
    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf

    def long(i: int) -> str:
        return f"{i}:" + "x" * 220

    state.messages[buf.buffer_id] = [
        _irc_message(11, msg_id=i, contents=long(i)) for i in range(51, 101)
    ]
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 24)) as pilot:
        log = await _open_buffer(pilot, app, client)

        log.scroll_to(y=20, animate=False, immediate=True)
        await pilot.pause()
        assert not log.is_vertical_scroll_end
        top_before = _top_visible_id(log)

        older = [_irc_message(11, msg_id=i, contents=long(i)) for i in range(1, 51)]
        state.messages[buf.buffer_id] = older + state.messages[buf.buffer_id]
        log.set_active_buffer(buf.buffer_id)
        await pilot.pause()

        assert _top_visible_id(log) == top_before


@pytest.mark.asyncio
async def test_anchor_skips_marker_row_so_marker_move_doesnt_yank() -> None:
    """Codex finding 2: never anchor on the synthetic marker row.

    The "read up to here" marker is a movable row with a fixed id. If the
    anchor is the marker and the marker then moves (empty-Enter advances
    it to the latest message), restoring "the same id" scrolls the
    viewport to wherever the marker went. The anchor must pin a stable
    *message* instead.
    """
    from quasseltui.app.widgets.message_log import _MARKER_OPTION_ID

    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = _lines(11, range(1, 101))
    # Marker on an early message → its row sits near the top.
    state.read_markers[buf.buffer_id] = MsgId(5)
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 24)) as pilot:
        log = await _open_buffer(pilot, app, client)

        # Render order is msg1..msg5, MARKER, msg6..msg100 — so the marker
        # row is at index 5. Scroll it to the very top of the viewport.
        log.scroll_to(y=5, animate=False, immediate=True)
        await pilot.pause()
        assert log.get_option_at_index(round(log.scroll_y)).id == _MARKER_OPTION_ID
        assert not log.is_vertical_scroll_end

        # Move the marker to the latest message and refresh (the path
        # empty-Enter / MarkerToLatestRequested drives).
        state.read_markers[buf.buffer_id] = MsgId(100)
        log.set_active_buffer(buf.buffer_id)
        await pilot.pause()

        # The viewport must NOT have chased the marker to the bottom.
        assert not log.is_vertical_scroll_end
        top_id = _top_visible_id(log)
        assert top_id is not None and top_id.startswith("msg:")
        assert int(top_id.split(":")[1]) <= 12


@pytest.mark.asyncio
async def test_send_failure_notice_is_sanitized() -> None:
    """Codex finding 3: notice text is sanitized and not parsed as markup.

    Send/backlog failure text comes from exceptions that can carry core
    strings with control bytes or Rich-markup brackets. `_notify_user`
    must strip control bytes (so they can't reach the terminal) and pass
    `markup=False` (so bracketed text can't crash or restyle the toast).
    """
    from quasseltui.app.widgets.input_bar import InputBar
    from quasseltui.protocol.errors import QuasselError

    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1)]

    class _FailingSendClient(_StubClient):
        async def send_input(
            self, buffer_id: BufferId, text: str
        ) -> None:  # pragma: no cover - trivial
            raise QuasselError("pipe \x1b[31mboom\x07")

    client = _FailingSendClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        input_bar = app.screen.query_one(InputBar)
        input_bar.value = "hi"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert app.notices
        notice = app.notices[-1]
        assert "\x1b" not in notice
        assert "\x07" not in notice
        assert "\\x1b" in notice  # escaped form is preserved for debugging
        assert app.return_code in (None, 0)


@pytest.mark.asyncio
async def test_no_backlog_request_after_disconnect() -> None:
    """Codex finding 4: stop requesting backlog once disconnected.

    After a mid-session drop the client is dead, but buffer switches
    (alt+up/down still work) would keep spawning backlog requests that
    fail against the closed socket and spam "Could not load history".
    Backlog requests must be gated on the live connection.
    """
    state = _empty_state_with_one_network()
    a = _buffer(11, name="#a")
    b = _buffer(22, name="#b")
    state.buffers[a.buffer_id] = a
    state.buffers[b.buffer_id] = b
    state.messages[a.buffer_id] = [_irc_message(11, msg_id=1)]
    state.messages[b.buffer_id] = [_irc_message(22, msg_id=2)]

    requested: list[BufferId] = []

    class _BacklogClient(_StubClient):
        async def request_backlog(
            self, buffer_id: BufferId, limit: int = 100
        ) -> None:  # pragma: no cover - trivial
            requested.append(buffer_id)

    client = _BacklogClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        client.push_event(ClientDisconnected(reason="gone", error=None))
        await pilot.pause()
        await pilot.pause()
        assert app.connection_lost is True

        before = len(requested)
        other = b.buffer_id if app.active_buffer_id == a.buffer_id else a.buffer_id
        app.post_message(BufferSelected(buffer_id=other))
        await pilot.pause()
        await pilot.pause()

        assert app.active_buffer_id == other
        assert len(requested) == before


@pytest.mark.asyncio
async def test_live_message_at_tail_still_follows() -> None:
    """Regression guard: a reader sitting at the tail keeps following.

    The anchor logic must NOT break the common case — when you're at the
    bottom of a live channel, new messages should still scroll into view.
    """
    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = _lines(11, range(1, 101))
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 24)) as pilot:
        log = await _open_buffer(pilot, app, client)

        log.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        assert log.is_vertical_scroll_end

        state.messages[buf.buffer_id].append(_irc_message(11, msg_id=101, contents="newest"))
        log.set_active_buffer(buf.buffer_id)
        await pilot.pause()

        assert log.is_vertical_scroll_end


# ---------------------------------------------------------------------------
# Phase 8 — interactive buffer switching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tree_node_selection_switches_active_buffer() -> None:
    """Selecting a leaf in the `BufferTree` flips `active_buffer_id`.

    Regression for phase 6's "no interactivity" hole: clicking or
    pressing Enter on a channel in the sidebar now has to route
    through `on_tree_node_selected` → `BufferSelected` → app
    handler → `ActiveBufferUpdated`, ending with the message log
    showing that buffer's history. Without this, the user would
    see the default-pick buffer forever and have no way to
    navigate anywhere else.
    """
    state = _empty_state_with_one_network()
    first = _buffer(11, name="#python")
    second = _buffer(22, name="#rust")
    state.buffers[first.buffer_id] = first
    state.buffers[second.buffer_id] = second
    state.messages[first.buffer_id] = [_irc_message(11, msg_id=1, contents="first buffer line")]
    state.messages[second.buffer_id] = [_irc_message(22, msg_id=2, contents="second buffer line")]

    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        # Trigger a SessionOpened so the bridge runs default-pick and
        # lands on one of the buffers. The exact default-pick target
        # depends on dict ordering, so we assert the switch relative
        # to the post-default state rather than against a fixed id.
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()
        default_pick = app.active_buffer_id
        assert default_pick is not None

        # Find the "other" buffer — the one the default-pick did not land on.
        target = second.buffer_id if default_pick == first.buffer_id else first.buffer_id
        target_name = "#rust" if target == second.buffer_id else "#python"
        target_content = "second buffer line" if target == second.buffer_id else "first buffer line"

        tree = app.screen.query_one(BufferTree)
        # Walk the tree for the leaf whose data carries `target`. The
        # top-level children are network nodes; their children are the
        # per-buffer leaves we care about.
        target_leaf = None
        for network_node in tree.root.children:
            for leaf in network_node.children:
                if leaf.data is not None and leaf.data.buffer_id == target:
                    target_leaf = leaf
                    break
            if target_leaf is not None:
                break
        assert target_leaf is not None, f"no sidebar leaf for {target_name}"

        tree.select_node(target_leaf)
        await pilot.pause()
        await pilot.pause()

        assert app.active_buffer_id == target

        log = app.screen.query_one(MessageLog)
        rendered = " ".join(_option_text(log, i) for i in range(log.option_count))
        assert target_content in rendered


@pytest.mark.asyncio
async def test_alt_down_cycles_to_next_buffer() -> None:
    """`alt+down` moves the active buffer forward in tree order.

    Verifies the app-level cycle binding works even while the input
    bar has focus (which is the default, via `AUTO_FOCUS = "InputBar"`).
    The priority binding means Textual does not swallow the key as
    an input cursor-move. Without this the user would be stuck on
    the default-pick buffer when their cursor is in the input box.
    """
    state = _empty_state_with_one_network()
    buffers = [
        _buffer(11, name="#alpha"),
        _buffer(22, name="#beta"),
        _buffer(33, name="#gamma"),
    ]
    for buf in buffers:
        state.buffers[buf.buffer_id] = buf
        state.messages[buf.buffer_id] = [_irc_message(int(buf.buffer_id), msg_id=1)]

    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        before = app.active_buffer_id
        assert before is not None

        await pilot.press("alt+down")
        await pilot.pause()

        after = app.active_buffer_id
        assert after is not None
        assert after != before

        # After a full cycle, we should be back at the starting buffer.
        await pilot.press("alt+down")
        await pilot.press("alt+down")
        await pilot.pause()
        assert app.active_buffer_id == before


@pytest.mark.asyncio
async def test_input_submit_calls_client_send_input() -> None:
    """Enter in the input bar routes text through `client.send_input`.

    The full chain is InputBar.on_input_submitted → post
    `InputSubmitted` → app.`_on_input_submitted` → client.send_input.
    Without this end-to-end test, a regression anywhere in that
    chain (wrong message class, missing handler, wrong attribute)
    would silently break outbound chat — the kind of bug that only
    shows up when a user tries to talk and gets no response.
    """
    from quasseltui.app.widgets.input_bar import InputBar

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1)]

    sent: list[tuple[BufferId, str]] = []

    class _SendingStubClient(_StubClient):
        async def send_input(
            self, buffer_id: BufferId, text: str
        ) -> None:  # pragma: no cover - trivial
            sent.append((buffer_id, text))

    client = _SendingStubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()
        assert app.active_buffer_id == buf.buffer_id

        input_bar = app.screen.query_one(InputBar)
        input_bar.value = "hello from tests"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

    assert sent == [(buf.buffer_id, "hello from tests")]


@pytest.mark.asyncio
async def test_input_text_survives_send_failure() -> None:
    """Failed sends must restore the typed line in the input bar.

    The widget clears eagerly on Enter (to prevent duplicate submits)
    and the app restores the text when `send_input` raises a
    `QuasselError`. Without the restore, a transient failure would
    eat the user's text with no way to retry.
    """
    from quasseltui.app.widgets.input_bar import InputBar
    from quasseltui.protocol.errors import QuasselError

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1)]

    class _FailingSendClient(_StubClient):
        async def send_input(
            self, buffer_id: BufferId, text: str
        ) -> None:  # pragma: no cover - trivial
            raise QuasselError("simulated broken pipe")

    client = _FailingSendClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()
        assert app.active_buffer_id == buf.buffer_id

        input_bar = app.screen.query_one(InputBar)
        input_bar.value = "retry me"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        # Critical: the typed line must still be there so the user
        # can hit Enter again after a reconnect instead of having to
        # retype it from memory.
        assert input_bar.value == "retry me"


@pytest.mark.asyncio
async def test_input_text_clears_on_successful_send() -> None:
    """Complement to `test_input_text_survives_send_failure`: a
    successful send MUST clear the input. Without this test a
    regression where the clear was removed entirely (or moved to the
    wrong branch) would silently ship — the failure-path test above
    would still pass because nothing was ever cleared anywhere.
    """
    from quasseltui.app.widgets.input_bar import InputBar

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1)]

    class _WorkingSendClient(_StubClient):
        async def send_input(
            self, buffer_id: BufferId, text: str
        ) -> None:  # pragma: no cover - trivial
            return None

    client = _WorkingSendClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        input_bar = app.screen.query_one(InputBar)
        input_bar.value = "send me"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert input_bar.value == ""


@pytest.mark.asyncio
async def test_tree_cursor_follows_bridge_driven_default_pick() -> None:
    """Regression for codex-review finding: the tree cursor used to
    drift out of sync on bridge-driven active-buffer changes.

    The bridge writes `active_buffer_id` directly during default-pick
    (first event that populates state) and removal-recovery. Before
    the fix only `_set_active_buffer` updated the tree, so the first
    buffer the bridge landed on would leave the sidebar cursor on
    the first leaf regardless of which buffer the message log was
    showing. The fix folds tree-sync into `_on_active_buffer_updated`
    so every `ActiveBufferUpdated` message — from any source — keeps
    the sidebar visual consistent with the message log.
    """
    state = _empty_state_with_one_network()
    # Two buffers; seed messages only on the second so the
    # "prefer buffers with content" default-pick heuristic skips the
    # first and lands on the second — that's the bit that would have
    # left the tree cursor stuck on #alpha before the fix.
    buffers = [
        _buffer(11, name="#alpha"),
        _buffer(22, name="#beta"),
    ]
    for buf in buffers:
        state.buffers[buf.buffer_id] = buf
        state.messages[buf.buffer_id] = []
    state.messages[buffers[1].buffer_id] = [_irc_message(22, msg_id=1)]

    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        assert app.active_buffer_id == buffers[1].buffer_id

        tree = app.screen.query_one(BufferTree)
        cursor = tree.cursor_node
        assert cursor is not None
        assert cursor.data is not None
        assert cursor.data.buffer_id == buffers[1].buffer_id


@pytest.mark.asyncio
async def test_input_submit_is_noop_when_no_active_buffer() -> None:
    """A stray Enter before any buffer is picked must not crash.

    Guards the handler's `active_buffer_id is None` branch. Without
    the guard, `send_input(None, text)` would raise a TypeError
    inside Textual's message machinery — not fatal to the app but
    ugly, and it would pollute the log with a spurious traceback.
    """
    from quasseltui.app.widgets.input_bar import InputBar

    state = _empty_state_with_one_network()  # zero buffers, so no default-pick
    sent: list[tuple[BufferId, str]] = []

    class _SendingStubClient(_StubClient):
        async def send_input(
            self, buffer_id: BufferId, text: str
        ) -> None:  # pragma: no cover - must not be called
            sent.append((buffer_id, text))

    client = _SendingStubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.screen.query_one(InputBar)
        input_bar.value = "stranded"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

    assert sent == []


@pytest.mark.asyncio
async def test_rapid_double_enter_sends_only_once() -> None:
    """Regression for codex-review finding: two rapid Enters could
    enqueue the same line twice before the first ``send_input``
    completed. The fix is to clear the widget eagerly on Enter so the
    second press sees an empty value and is dropped by InputBar's
    empty-line guard.
    """
    from quasseltui.app.widgets.input_bar import InputBar

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1)]

    sent: list[tuple[BufferId, str]] = []

    class _SendingStubClient(_StubClient):
        async def send_input(
            self, buffer_id: BufferId, text: str
        ) -> None:  # pragma: no cover - trivial
            sent.append((buffer_id, text))

    client = _SendingStubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        input_bar = app.screen.query_one(InputBar)
        input_bar.value = "only once"
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

    assert sent == [(buf.buffer_id, "only once")]


@pytest.mark.asyncio
async def test_last_buffer_removed_clears_tree_cursor() -> None:
    """Regression for codex-review finding: when the last active
    buffer is removed, the bridge emits ``ActiveBufferUpdated(None)``.
    Without handling ``None`` in the tree, the sidebar retains stale
    selection state while the message log has already cleared.
    """
    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1)]

    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        assert app.active_buffer_id == buf.buffer_id
        tree = app.screen.query_one(BufferTree)
        assert tree.cursor_node is not None

        # Simulate a buffer removal: the dispatcher removes it from
        # state, then the bridge handles the event.
        del state.buffers[buf.buffer_id]
        del state.messages[buf.buffer_id]
        client.push_event(BufferRemoved(buffer_id=buf.buffer_id))
        await pilot.pause()
        await pilot.pause()

        assert app.active_buffer_id is None
        # After all buffers are removed, the tree's _active_hint must
        # be cleared. Textual's Tree may land the cursor on the
        # network header (data=None) rather than nowhere, so we
        # assert no buffer leaf is selected rather than cursor=None.
        assert tree._active_hint is None
        cursor = tree.cursor_node
        if cursor is not None:
            assert cursor.data is None


@pytest.mark.asyncio
async def test_alt_up_cycles_to_previous_buffer() -> None:
    """`alt+up` is the mirror of `alt+down`.

    Separately covered (rather than piggybacking on the `alt+down`
    test) so a regression where we accidentally swap the deltas
    lands on one of these two tests instead of silently passing.
    """
    state = _empty_state_with_one_network()
    buffers = [_buffer(11, name="#alpha"), _buffer(22, name="#beta")]
    for buf in buffers:
        state.buffers[buf.buffer_id] = buf
        state.messages[buf.buffer_id] = [_irc_message(int(buf.buffer_id), msg_id=1)]

    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        before = app.active_buffer_id
        assert before is not None
        await pilot.press("alt+up")
        await pilot.pause()
        after = app.active_buffer_id
        assert after is not None
        assert after != before


# ---------------------------------------------------------------------------
# Read-up-to-here marker
# ---------------------------------------------------------------------------


def _option_ids(log: MessageLog) -> list[str | None]:
    """Snapshot every option id in render order.

    Used by the marker tests to assert "did a marker row appear in
    the right place?" without depending on the exact option prompt
    text or formatting.
    """
    return [log.get_option_at_index(i).id for i in range(log.option_count)]


@pytest.mark.asyncio
async def test_enter_on_message_row_places_marker_in_state() -> None:
    """Pressing Enter while a message is highlighted writes to
    `state.read_markers` and inserts a marker row after that message.

    This is the core of the feature: the user Tabs into the message
    log, walks to a row, hits Enter, and the marker anchors there so
    they can recognise "everything above this line is old" on their
    next glance at the channel.
    """
    from quasseltui.app.widgets.message_log import _MARKER_OPTION_ID

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [
        _irc_message(11, msg_id=1, contents="first"),
        _irc_message(11, msg_id=2, contents="second"),
        _irc_message(11, msg_id=3, contents="third"),
    ]

    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()
        assert app.active_buffer_id == buf.buffer_id

        log = app.screen.query_one(MessageLog)
        # Focus the log and highlight the middle message (msg_id=2),
        # then press Enter.
        log.focus()
        log.highlighted = 1
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert state.read_markers.get(buf.buffer_id) == MsgId(2)

        ids = _option_ids(log)
        # Three messages plus one marker = four rows. The marker row
        # sits directly after the option whose id encodes msg_id=2.
        assert len(ids) == 4
        assert ids[0] == "msg:1"
        assert ids[1] == "msg:2"
        assert ids[2] == _MARKER_OPTION_ID
        assert ids[3] == "msg:3"


@pytest.mark.asyncio
async def test_tabbing_to_log_auto_highlights_last_message() -> None:
    """Focus on the log with no prior cursor must snap to the last
    message so Enter immediately places a marker.

    Without this, the user Tabs into the log, sees the focus border
    but no row cursor, presses Enter, and `OptionList.action_select`
    silently returns (it requires `highlighted` to be non-None). The
    user's perception is "I pressed Enter on a blank message and
    nothing happened"; in fact they pressed it on nothing at all.
    """
    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [
        _irc_message(11, msg_id=1, contents="oldest"),
        _irc_message(11, msg_id=2, contents="middle"),
        _irc_message(11, msg_id=3, contents="newest"),
    ]

    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        log = app.screen.query_one(MessageLog)
        # Before focus: no highlight (set_active_buffer on a switch
        # resets highlighted to None).
        assert log.highlighted is None

        # Drive focus via a real keystroke, not `log.focus()` — the
        # user only ever gets into the log by tabbing, so the test
        # exercises the same path. The screen auto-focuses InputBar;
        # shift+tab walks the focus chain backwards to MessageLog in
        # one step (DOM order is BufferTree → MessageLog → InputBar,
        # so shift+tab from InputBar lands on MessageLog).
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.focused is log, (
            f"shift+tab did not focus MessageLog (got {type(app.focused).__name__})"
        )

        # After focus: highlighted snapped to the last message.
        assert log.highlighted == 2
        assert log.get_option_at_index(log.highlighted).id == "msg:3"

        # And pressing Enter immediately fires the marker placement.
        await pilot.press("enter")
        await pilot.pause()
        assert state.read_markers.get(buf.buffer_id) == MsgId(3)


@pytest.mark.asyncio
async def test_placing_new_marker_replaces_previous_one() -> None:
    """A second Enter on a different row moves the marker — it never
    leaves two marker rows in the same buffer. This is the "if any
    previous 'read up to' marker is in the backlog remove it" half of
    the feature.
    """
    from quasseltui.app.widgets.message_log import _MARKER_OPTION_ID

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [
        _irc_message(11, msg_id=1, contents="one"),
        _irc_message(11, msg_id=2, contents="two"),
        _irc_message(11, msg_id=3, contents="three"),
    ]

    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        log = app.screen.query_one(MessageLog)
        log.focus()
        log.highlighted = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        # First marker landed after msg_id=1.
        assert state.read_markers[buf.buffer_id] == MsgId(1)
        ids_first = _option_ids(log)
        assert ids_first.count(_MARKER_OPTION_ID) == 1
        assert ids_first.index(_MARKER_OPTION_ID) == 1

        # Now move to the last message (OptionList shifted its indices
        # by one to accommodate the marker row, so msg_id=3 is at 3).
        log.highlighted = log.get_option_index("msg:3")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert state.read_markers[buf.buffer_id] == MsgId(3)
        ids_second = _option_ids(log)
        # Still exactly one marker — the old one was removed when we
        # rebuilt the option list from state.read_markers.
        assert ids_second.count(_MARKER_OPTION_ID) == 1
        # And it now sits after msg_id=3.
        marker_idx = ids_second.index(_MARKER_OPTION_ID)
        assert ids_second[marker_idx - 1] == "msg:3"


@pytest.mark.asyncio
async def test_marker_is_per_buffer_not_global() -> None:
    """Setting a marker in one buffer must NOT affect another buffer's
    marker state. This is the "per-buffer" contract the user asked for.
    """
    state = _empty_state_with_one_network()
    buf_a = _buffer(11, name="#alpha")
    buf_b = _buffer(22, name="#beta")
    state.buffers[buf_a.buffer_id] = buf_a
    state.buffers[buf_b.buffer_id] = buf_b
    state.messages[buf_a.buffer_id] = [_irc_message(11, msg_id=1, contents="a1")]
    state.messages[buf_b.buffer_id] = [_irc_message(22, msg_id=7, contents="b1")]

    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        # Ensure we start on #alpha, then drop a marker there.
        app.post_message(BufferSelected(buffer_id=buf_a.buffer_id))
        await pilot.pause()
        await pilot.pause()
        assert app.active_buffer_id == buf_a.buffer_id

        log = app.screen.query_one(MessageLog)
        log.focus()
        log.highlighted = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert state.read_markers.get(buf_a.buffer_id) == MsgId(1)
        assert buf_b.buffer_id not in state.read_markers


@pytest.mark.asyncio
async def test_empty_enter_in_input_bar_places_marker_at_latest_message() -> None:
    """Pressing Enter in the InputBar with no text drops the marker at
    the newest message in the active buffer.

    Complements `test_enter_on_message_row_places_marker_in_state`:
    that one covers the Tab-into-log path, this one covers the
    keyboard-only user who never leaves the input. The screen's
    `AUTO_FOCUS = "InputBar"` means an untouched app has focus on the
    input, so pressing Enter with an empty value routes through
    `InputBar.on_input_submitted` → `MarkerToLatestRequested` →
    `QuasselApp._on_marker_to_latest_requested`, which writes the last
    message's id into `state.read_markers`.
    """
    from quasseltui.app.widgets.input_bar import InputBar
    from quasseltui.app.widgets.message_log import _MARKER_OPTION_ID

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [
        _irc_message(11, msg_id=1, contents="oldest"),
        _irc_message(11, msg_id=2, contents="middle"),
        _irc_message(11, msg_id=3, contents="newest"),
    ]

    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()
        assert app.active_buffer_id == buf.buffer_id

        input_bar = app.screen.query_one(InputBar)
        # Sanity: focus defaults to the InputBar and its value is empty.
        assert app.focused is input_bar
        assert input_bar.value == ""

        await pilot.press("enter")
        await pilot.pause()

        assert state.read_markers.get(buf.buffer_id) == MsgId(3)

        log = app.screen.query_one(MessageLog)
        ids = _option_ids(log)
        # Three messages plus the marker row, with the marker at the
        # tail because it anchors on the newest message.
        assert ids == ["msg:1", "msg:2", "msg:3", _MARKER_OPTION_ID]


@pytest.mark.asyncio
async def test_empty_enter_with_no_messages_is_a_noop() -> None:
    """If the active buffer has no messages, empty Enter does nothing.

    There's no MsgId to anchor on, and writing a marker whose message
    has not yet arrived would render as a floating separator with no
    line above it once the first message lands. The handler silently
    skips instead.
    """
    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = []

    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()
        # The default-picker requires non-empty messages, so the app
        # stays with no active buffer here — exercising the "no active
        # buffer" branch of the handler too.
        await pilot.press("enter")
        await pilot.pause()

        assert state.read_markers == {}
        log = app.screen.query_one(MessageLog)
        assert log.option_count == 0


@pytest.mark.asyncio
async def test_non_empty_enter_does_not_place_marker() -> None:
    """Enter with text still submits via `LineSubmitted` — no marker.

    Guards against a regression where empty-Enter's new branch
    accidentally falls through for non-empty input and drops a
    phantom marker on every sent line.
    """
    from quasseltui.app.widgets.input_bar import InputBar

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1, contents="hi")]

    sent: list[tuple[BufferId, str]] = []

    class _SendingStubClient(_StubClient):
        async def send_input(
            self, buffer_id: BufferId, text: str
        ) -> None:  # pragma: no cover - trivial
            sent.append((buffer_id, text))

    client = _SendingStubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        input_bar = app.screen.query_one(InputBar)
        input_bar.value = "hello"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert sent == [(buf.buffer_id, "hello")]
        assert state.read_markers == {}
        assert input_bar.value == ""


@pytest.mark.asyncio
async def test_ctrl_r_reconnects_after_disconnect() -> None:
    """Ctrl+R after a mid-session drop must build a fresh client from the
    factory, restart the bridge, clear the disconnected latch, re-enable
    the input bar, and restore any text the user had typed when the
    connection died. Before this, the placeholder literally told the
    user to quit — the only recovery was restarting the whole app and
    re-entering the password.
    """
    from quasseltui.app.widgets.input_bar import InputBar

    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = []
    client = _StubClient(state)
    replacement = _StubClient(state)
    factory_calls: list[int] = []

    def factory() -> _StubClient:
        factory_calls.append(1)
        return replacement

    app = QuasselApp(state, client=client, client_factory=factory)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()

        input_bar = app.screen.query_one(InputBar)
        input_bar.value = "draft line"
        client.push_event(ClientDisconnected(reason="core went away", error=None))
        await pilot.pause()
        await pilot.pause()
        assert app.connection_lost is True
        assert input_bar.disabled is True
        # The pending text is stashed so the placeholder (which Textual
        # renders only when value is empty) is actually visible — and
        # the placeholder names the remedy.
        assert input_bar.value == ""
        assert "Ctrl+R" in input_bar.placeholder

        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.pause()
        assert factory_calls == [1]
        assert client.closed is True
        assert app.connection_lost is False
        assert input_bar.disabled is False
        assert input_bar.placeholder == InputBar.DEFAULT_PLACEHOLDER
        assert input_bar.value == "draft line"

        # The new bridge is live: a session event over the replacement
        # client reaches the app.
        replacement.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()
        assert app.connection_lost is False


@pytest.mark.asyncio
async def test_ctrl_r_while_connected_does_not_rebuild_client() -> None:
    """Reconnect is a recovery action: while the session is healthy it
    must not tear down a working connection."""
    state = _empty_state_with_one_network()
    client = _StubClient(state)
    factory_calls: list[int] = []

    def factory() -> _StubClient:
        factory_calls.append(1)
        return _StubClient(state)

    app = QuasselApp(state, client=client, client_factory=factory)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert factory_calls == []
        assert client.closed is False


@pytest.mark.asyncio
async def test_ctrl_r_without_factory_is_a_safe_noop() -> None:
    """ui-demo (no client) and any embedder that didn't pass a factory
    must not crash on Ctrl+R."""
    state = _empty_state_with_one_network()
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        client.push_event(ClientDisconnected(reason="gone", error=None))
        await pilot.pause()
        await pilot.pause()
        assert app.connection_lost is True
        await pilot.press("ctrl+r")
        await pilot.pause()
        # Still disconnected, no crash, no replacement client.
        assert app.connection_lost is True


@pytest.mark.asyncio
async def test_switching_buffers_reports_last_seen_to_core() -> None:
    """Leaving a buffer must push requestSetLastSeenMsg to the core so
    reading in quasseltui marks the buffer read in every other Quassel
    client. Before this, read state never round-tripped at all."""
    state = _empty_state_with_one_network()
    buf_a = _buffer(11, name="#a")
    buf_b = _buffer(12, name="#b")
    state.buffers[buf_a.buffer_id] = buf_a
    state.buffers[buf_b.buffer_id] = buf_b
    state.messages[buf_a.buffer_id] = [_irc_message(11, msg_id=5, contents="latest")]
    state.messages[buf_b.buffer_id] = []
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()
        # Default pick lands on #a (the buffer with content).
        assert app.active_buffer_id == buf_a.buffer_id

        app.post_message(BufferSelected(buffer_id=buf_b.buffer_id))
        await pilot.pause()
        await pilot.pause()
        assert app.active_buffer_id == buf_b.buffer_id
        assert client.last_seen_calls == [(buf_a.buffer_id, MsgId(5))]


@pytest.mark.asyncio
async def test_marker_placement_syncs_marker_line_to_core() -> None:
    """A locally placed read marker must round-trip to the core via
    requestSetMarkerLine so it persists across runs and other clients."""
    from quasseltui.app.messages import MarkerToLatestRequested

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#a")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=7, contents="newest")]
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()
        assert app.active_buffer_id == buf.buffer_id

        app.post_message(MarkerToLatestRequested())
        await pilot.pause()
        await pilot.pause()
        assert client.marker_calls == [(buf.buffer_id, MsgId(7))]


@pytest.mark.asyncio
async def test_date_separator_rows_on_day_boundaries() -> None:
    """Multi-day backlog rendered with HH:MM:SS-only timestamps is
    indistinguishable from today's traffic. The log must insert a
    disabled date-separator row before messages from a day other than
    'today' and at each day boundary."""
    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    old = _irc_message(11, msg_id=1, contents="two days ago")
    older_ts = dt.datetime(2026, 4, 14, 9, 0, 0, tzinfo=dt.UTC)
    newer_ts = dt.datetime(2026, 4, 15, 9, 0, 0, tzinfo=dt.UTC)
    from dataclasses import replace

    state.messages[buf.buffer_id] = [
        replace(old, timestamp=older_ts),
        replace(_irc_message(11, msg_id=2, contents="next day"), timestamp=newer_ts),
    ]
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        log = app.screen.query_one(MessageLog)
        rows = [_option_text(log, i) for i in range(log.option_count)]
        disabled = [
            _option_text(log, i)
            for i in range(log.option_count)
            if log.get_option_at_index(i).disabled
        ]
        # One separator before the first (non-today) message, one at the
        # 14th -> 15th boundary.
        assert sum("2026-04-14" in row for row in disabled) == 1
        assert sum("2026-04-15" in row for row in disabled) == 1
        # Separators are positioned before their day's messages.
        assert rows.index(next(r for r in rows if "2026-04-14" in r)) < rows.index(
            next(r for r in rows if "two days ago" in r)
        )
        assert rows.index(next(r for r in rows if "2026-04-15" in r)) < rows.index(
            next(r for r in rows if "next day" in r)
        )


@pytest.mark.asyncio
async def test_click_in_scrollback_highlights_but_does_not_move_marker() -> None:
    """OptionList's built-in click handler used to call action_select()
    for every left click, silently relocating the read marker on focus
    clicks and failed text-selection attempts. Clicks must highlight
    only; marker placement stays on Enter."""
    from types import SimpleNamespace

    from rich.style import Style

    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [
        _irc_message(11, msg_id=1, contents="first"),
        _irc_message(11, msg_id=2, contents="second"),
    ]
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        log = app.screen.query_one(MessageLog)
        # Find the index of a non-disabled message row to "click".
        target = next(i for i in range(log.option_count) if not log.get_option_at_index(i).disabled)
        event = SimpleNamespace(
            style=Style(meta={"option": target}),
            prevent_default=lambda: None,
        )
        await log._on_click(event)  # type: ignore[arg-type]
        await pilot.pause()
        await pilot.pause()

        assert log.highlighted == target
        assert state.read_markers == {}


@pytest.mark.asyncio
async def test_input_history_up_down_recall() -> None:
    """Up/Down in the input bar must recall previously sent lines —
    muscle memory for every IRC user and previously entirely absent."""
    from quasseltui.app.widgets.input_bar import InputBar

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1)]

    class _SendingStubClient(_StubClient):
        async def send_input(self, buffer_id: BufferId, text: str) -> None:
            pass

    client = _SendingStubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        input_bar = app.screen.query_one(InputBar)
        input_bar.focus()
        input_bar.value = "first line"
        await pilot.press("enter")
        await pilot.pause()
        input_bar.value = "second line"
        await pilot.press("enter")
        await pilot.pause()
        assert input_bar.value == ""

        await pilot.press("up")
        assert input_bar.value == "second line"
        await pilot.press("up")
        assert input_bar.value == "first line"
        # Clamped at the oldest entry.
        await pilot.press("up")
        assert input_bar.value == "first line"
        await pilot.press("down")
        assert input_bar.value == "second line"
        # Walking past the newest restores the (empty) in-progress text.
        await pilot.press("down")
        assert input_bar.value == ""


@pytest.mark.asyncio
async def test_input_history_preserves_in_progress_text() -> None:
    from quasseltui.app.widgets.input_bar import InputBar

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1)]

    class _SendingStubClient(_StubClient):
        async def send_input(self, buffer_id: BufferId, text: str) -> None:
            pass

    client = _SendingStubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        input_bar = app.screen.query_one(InputBar)
        input_bar.focus()
        input_bar.value = "sent already"
        await pilot.press("enter")
        await pilot.pause()

        input_bar.value = "half typed"
        await pilot.press("up")
        assert input_bar.value == "sent already"
        await pilot.press("down")
        assert input_bar.value == "half typed"


@pytest.mark.asyncio
async def test_whitespace_only_input_is_not_sent() -> None:
    """A space + Enter accident must not broadcast a blank-looking
    message to the whole channel (and must not move the marker)."""
    from quasseltui.app.widgets.input_bar import InputBar

    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1)]

    sent: list[tuple[BufferId, str]] = []

    class _SendingStubClient(_StubClient):
        async def send_input(self, buffer_id: BufferId, text: str) -> None:
            sent.append((buffer_id, text))

    client = _SendingStubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        input_bar = app.screen.query_one(InputBar)
        input_bar.focus()
        input_bar.value = "   "
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert sent == []
        assert state.read_markers == {}
        assert input_bar.value == ""


@pytest.mark.asyncio
async def test_empty_enter_marker_move_shows_notice() -> None:
    """Empty Enter silently overwrote the previous read marker with no
    feedback or undo; at minimum the user must SEE that it happened."""
    state = _empty_state_with_one_network()
    buf = _buffer(11, name="#python")
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=3, contents="latest")]
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()
        from quasseltui.app.messages import MarkerToLatestRequested

        app.post_message(MarkerToLatestRequested())
        await pilot.pause()
        await pilot.pause()
        assert state.read_markers[buf.buffer_id] == MsgId(3)
        assert any("marker" in notice.lower() for notice in app.notices)


@pytest.mark.asyncio
async def test_line_typed_before_active_buffer_is_restored_with_notice() -> None:
    """Typing during the startup window (input focused, handshake still
    settling) used to silently discard the line — the widget had
    already cleared itself. Restore the text and tell the user."""
    from quasseltui.app.widgets.input_bar import InputBar

    state = _empty_state_with_one_network()
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        # No SessionOpened pushed: active_buffer_id is still None.
        assert app.active_buffer_id is None
        input_bar = app.screen.query_one(InputBar)
        input_bar.focus()
        input_bar.value = "typed too early"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert input_bar.value == "typed too early"
        assert any("buffer" in notice.lower() for notice in app.notices)


@pytest.mark.asyncio
async def test_anchor_capture_failure_falls_back_to_tail_not_top() -> None:
    """When the private-Textual-API anchor capture is unavailable, a
    scrolled-up refresh used to make NO scroll call at all — and since
    the rebuild resets scroll to 0, the reader got dumped at the TOP
    of the buffer. Tail is the documented degraded behavior."""
    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [
        _irc_message(11, msg_id=i, contents=f"line {i}") for i in range(1, 40)
    ]
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        log = app.screen.query_one(MessageLog)
        log.scroll_to(y=5, animate=False, immediate=True)
        await pilot.pause()
        assert not log.is_vertical_scroll_end

        # Simulate the guarded private-API access coming up empty.
        log._capture_scroll_anchor = lambda: None  # type: ignore[method-assign]
        state.messages[buf.buffer_id].append(_irc_message(11, msg_id=99, contents="new"))
        client.push_event(MessageReceived(message=_irc_message(11, msg_id=99, contents="new")))
        await pilot.pause(0.1)
        await pilot.pause()

        assert log.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_message_for_inactive_buffer_marks_activity_until_visited() -> None:
    """Messages for non-active buffers used to vanish from the UI path
    entirely — no styling, no counter, nothing. The sidebar must mark
    the buffer (highlight-aware) and clear when the user visits it."""
    state = _empty_state_with_one_network()
    buf_a = _buffer(11, name="#a")
    buf_b = _buffer(12, name="#b")
    state.buffers[buf_a.buffer_id] = buf_a
    state.buffers[buf_b.buffer_id] = buf_b
    state.messages[buf_a.buffer_id] = [_irc_message(11, msg_id=1)]
    state.messages[buf_b.buffer_id] = []
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()
        assert app.active_buffer_id == buf_a.buffer_id

        # Live message for the NON-active buffer.
        state.messages[buf_b.buffer_id].append(_irc_message(12, msg_id=2, contents="psst"))
        client.push_event(MessageReceived(message=_irc_message(12, msg_id=2, contents="psst")))
        await pilot.pause(0.1)
        await pilot.pause()

        assert app.buffer_activity.get(buf_b.buffer_id) == "message"
        tree = app.screen.query_one(BufferTree)
        assert tree.activity_level(buf_b.buffer_id) == "message"

        # Visiting the buffer clears the indicator.
        app.post_message(BufferSelected(buffer_id=buf_b.buffer_id))
        await pilot.pause()
        await pilot.pause()
        assert buf_b.buffer_id not in app.buffer_activity
        assert tree.activity_level(buf_b.buffer_id) is None


@pytest.mark.asyncio
async def test_failed_reconnect_relatches_instead_of_exiting() -> None:
    """Ctrl+R against a still-down core produced a pre-session
    disconnect, which the fresh bridge stamped fatal=True — exiting the
    entire app with return code 1 and destroying the scrollback the
    disconnected state had deliberately preserved. A failed reconnect
    must re-enter the disconnected state so the user can retry."""
    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = []
    client = _StubClient(state)
    replacement = _StubClient(state)
    app = QuasselApp(state, client=client, client_factory=lambda: replacement)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        client.push_event(ClientDisconnected(reason="core went away", error=None))
        await pilot.pause()
        await pilot.pause()
        assert app.connection_lost is True

        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.connection_lost is False

        # The replacement client never reaches SessionOpened — the core
        # is still down.
        replacement.push_event(ClientDisconnected(reason="connect refused", error=None))
        await pilot.pause()
        await pilot.pause()
        assert app.return_code in (None, 0)  # app did NOT exit
        assert app.connection_lost is True  # re-latched; Ctrl+R again works


@pytest.mark.asyncio
async def test_click_dispatch_prevents_base_optionlist_select() -> None:
    """The _on_click override alone does not stop Textual from ALSO
    running OptionList's own _on_click (handlers run across the MRO),
    which calls action_select() and moves the marker anyway. The
    override must call event.prevent_default()."""
    from types import SimpleNamespace

    from rich.style import Style

    state = _empty_state_with_one_network()
    buf = _buffer(11)
    state.buffers[buf.buffer_id] = buf
    state.messages[buf.buffer_id] = [_irc_message(11, msg_id=1, contents="row")]
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()
        log = app.screen.query_one(MessageLog)
        target = next(i for i in range(log.option_count) if not log.get_option_at_index(i).disabled)
        prevented: list[bool] = []
        event = SimpleNamespace(
            style=Style(meta={"option": target}),
            prevent_default=lambda: prevented.append(True),
        )
        await log._on_click(event)  # type: ignore[arg-type]
        assert prevented == [True]


@pytest.mark.asyncio
async def test_tree_refresh_resyncs_activity_from_app() -> None:
    """Activity recorded before the tree mounts (or across rebuilds)
    must not drift: the sidebar refresh re-syncs from the app's
    authoritative dict."""
    state = _empty_state_with_one_network()
    buf_a = _buffer(11, name="#a")
    buf_b = _buffer(12, name="#b")
    state.buffers[buf_a.buffer_id] = buf_a
    state.buffers[buf_b.buffer_id] = buf_b
    state.messages[buf_a.buffer_id] = [_irc_message(11, msg_id=1)]
    state.messages[buf_b.buffer_id] = []
    client = _StubClient(state)
    app = QuasselApp(state, client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        client.push_event(SessionOpened(session=session, peer_features=frozenset()))
        await pilot.pause()
        await pilot.pause()

        # Simulate drift: activity recorded on the app while the tree
        # copy is empty (e.g. recorded pre-mount).
        app.buffer_activity[buf_b.buffer_id] = "highlight"
        from quasseltui.app.messages import BufferListUpdated

        app.post_message(BufferListUpdated())
        await pilot.pause()
        await pilot.pause()
        tree = app.screen.query_one(BufferTree)
        assert tree.activity_level(buf_b.buffer_id) == "highlight"
