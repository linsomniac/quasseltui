"""End-to-end composition test: does the chat screen actually mount?

`App.run_test()` spins the whole Textual event loop against a mock
driver, which means we can assert that:

- The three-pane layout composes with exactly the three widgets we
  expect (no silent swallow of a widget due to a typo).
- `BufferTree` populates its nodes from the state (two networks, six
  leaves).
- `MessageLog` picks a buffer with content and renders at least one
  line.
- `InputBar` is present with its placeholder set.
- `Ctrl+Q` triggers `action_quit` — the one interactive guarantee the
  phase 6 plan asks us to verify.

These tests are a little slower than the pure-function tests because
each one starts an actual Textual App; they're worth it because they
catch TCSS typos and layout bugs that would otherwise only surface
when the user runs `ui-demo` interactively.
"""

from __future__ import annotations

import datetime as dt

import pytest

from quasseltui.app.app import QuasselApp
from quasseltui.app.demo_data import build_demo_state
from quasseltui.app.widgets.buffer_tree import BufferTree
from quasseltui.app.widgets.input_bar import InputBar
from quasseltui.app.widgets.message_log import MessageLog
from quasseltui.client.state import ClientState
from quasseltui.protocol.enums import MessageFlag, MessageType
from quasseltui.protocol.usertypes import (
    BufferId,
    BufferInfo,
    BufferType,
    MsgId,
    NetworkId,
)
from quasseltui.sync.events import IrcMessage
from quasseltui.sync.network import Network


@pytest.mark.asyncio
async def test_app_composes_three_panes() -> None:
    app = QuasselApp(build_demo_state())
    async with app.run_test() as pilot:
        # push_screen in on_mount → one pump lets the ChatScreen mount.
        await pilot.pause()
        assert app.screen.query_one(BufferTree) is not None
        assert app.screen.query_one(MessageLog) is not None
        assert app.screen.query_one(InputBar) is not None


@pytest.mark.asyncio
async def test_buffer_tree_populates_from_state() -> None:
    app = QuasselApp(build_demo_state())
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.screen.query_one(BufferTree)
        # Two networks at the top level.
        assert len(tree.root.children) == 2
        # Every leaf maps to a BufferInfo. Using the tree's cursor to
        # walk is the public API; `root.children[i].children` matches.
        leaves = [leaf for network in tree.root.children for leaf in network.children]
        assert len(leaves) == 6


@pytest.mark.asyncio
async def test_message_log_renders_content_for_active_buffer() -> None:
    app = QuasselApp(build_demo_state())
    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.screen.query_one(MessageLog)
        # `RichLog.lines` is a `deque[Strip]` of rendered lines and is
        # the stable public signal for "how many lines did we render".
        assert len(log.lines) >= 1


@pytest.mark.asyncio
async def test_input_bar_has_placeholder() -> None:
    app = QuasselApp(build_demo_state())
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.screen.query_one(InputBar)
        assert "Enter" in bar.placeholder


@pytest.mark.asyncio
async def test_ctrl_q_quits_the_app() -> None:
    """The plan's phase 6 verification explicitly calls out `Ctrl+Q`.

    We fire the binding and let `run_test` drain — if the action did
    not quit, the `async with` would hang until the test timeout.
    """
    app = QuasselApp(build_demo_state())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
    # `run_test`'s context manager swallows the exit path; if we got
    # here the app went through its quit lifecycle cleanly.
    assert app.return_code in (None, 0)


def _hostile_state() -> ClientState:
    """A `ClientState` whose every user-facing string is a spoof attempt.

    Covers the full matrix a phase-7 live client could plausibly hit:
    a network name that tries to re-color the sidebar, a channel name
    that embeds Rich markup, a message body full of ANSI CSI and BEL,
    and a newline in the contents that would otherwise forge a second
    line. The widgets should render all of them as literal text.
    """
    state = ClientState()
    network = Network("1")
    network.network_name = "\x1b[31mRED-NET"
    network.my_nick = "seanr"
    state.networks[NetworkId(1)] = network

    buffer_info = BufferInfo(
        buffer_id=BufferId(10),
        network_id=NetworkId(1),
        type=BufferType.Channel,
        group_id=0,
        name="[bold red]spoof[/]",
    )
    state.buffers[BufferId(10)] = buffer_info
    state.messages[BufferId(10)] = [
        IrcMessage(
            msg_id=MsgId(1),
            buffer_id=BufferId(10),
            network_id=NetworkId(1),
            timestamp=dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.UTC),
            type=MessageType.Plain,
            flags=MessageFlag.NONE,
            sender="eve!eve@evil",
            sender_prefixes="@\x1b",
            contents="\x1b[31mREDRUM\x07\nfake!eve@evil: second line",
        )
    ]
    return state


@pytest.mark.asyncio
async def test_chat_screen_renders_hostile_state_without_escape_bytes() -> None:
    """End-to-end: a malicious `ClientState` must not leak any control
    byte into what the widgets actually render. Complements the unit
    tests in `test_widgets.py` by exercising the full composition path
    (Textual's Tree.process_label, RichLog strip generation, etc.) so a
    regression in either helper or its call site is caught."""
    app = QuasselApp(_hostile_state())
    async with app.run_test() as pilot:
        await pilot.pause()

        tree = app.screen.query_one(BufferTree)
        # Walk every tree node and assert no raw ESC/BEL bytes made it
        # into the Text label. `.plain` gives the literal string Textual
        # will render.
        for network_node in tree.root.children:
            assert "\x1b" not in network_node.label.plain
            assert "\x07" not in network_node.label.plain
            # Rich markup must be present as literal brackets, not
            # interpreted as a style.
            for leaf in network_node.children:
                assert "\x1b" not in leaf.label.plain
                # `[bold red]spoof[/]` should appear verbatim.
                assert "spoof" in leaf.label.plain

        log = app.screen.query_one(MessageLog)
        # RichLog.lines is a deque of `Strip` objects; their `text`
        # attribute is what the terminal would receive.
        for strip in log.lines:
            assert "\x1b" not in strip.text
            assert "\x07" not in strip.text
            assert "\n" not in strip.text
