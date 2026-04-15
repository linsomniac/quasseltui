"""Main 3-pane chat screen.

```
┌───────────┬──────────────────────┐
│           │ message log          │
│  buffer   │                      │
│  tree     │                      │
│           │                      │
│           ├──────────────────────┤
│           │ input bar            │
└───────────┴──────────────────────┘
```

The screen owns widget identities, not their data: every widget takes a
`ClientState` reference and renders from it directly. Phase 7 swaps the
static state (from `demo_data.build_demo_state`) for the live
`QuasselClient.state`, and phase 6's widget code does not change.

Nick list pane is deliberately absent — the plan defers it to phase 11
because its data model (`IrcChannel.user_modes`) is messy and we don't
want it blocking the WOW-moment milestone.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen

from quasseltui.app.widgets.buffer_tree import BufferTree
from quasseltui.app.widgets.input_bar import InputBar
from quasseltui.app.widgets.message_log import MessageLog
from quasseltui.client.state import ClientState


class ChatScreen(Screen[None]):
    """Three-pane chat screen — buffer tree · message log + input."""

    def __init__(self, state: ClientState) -> None:
        super().__init__()
        self._state = state

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield BufferTree(self._state, id="buffer-tree")
            with Vertical(id="main-column"):
                yield MessageLog(self._state, id="message-log")
                yield InputBar(id="input-bar")


__all__ = [
    "ChatScreen",
]
