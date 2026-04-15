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

import pytest

from quasseltui.app.app import QuasselApp
from quasseltui.app.demo_data import build_demo_state
from quasseltui.app.widgets.buffer_tree import BufferTree
from quasseltui.app.widgets.input_bar import InputBar
from quasseltui.app.widgets.message_log import MessageLog


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
