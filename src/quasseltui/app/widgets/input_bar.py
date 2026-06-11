"""Bottom-pane input prompt.

On Enter with text, the current line is posted to the app as a
`LineSubmitted` message and the widget's value is cleared immediately.
If the app's ``send_input`` fails, the app restores the text so the
user can retry. Clearing eagerly (rather than waiting for a success
callback) closes the duplicate-submit window that would otherwise
exist between two rapid Enter presses — without this, the same line
could be queued twice before the first ``send_input`` finishes,
violating the single-writer assumption documented in ``connection.py``.

On Enter with no text, the widget posts `MarkerToLatestRequested`
instead. The app interprets that as "drop a read-up-to-here marker at
the newest message in the active buffer", which mirrors the marker
path that fires when a user Tabs into the log and presses Enter on a
highlighted row — but lets the keyboard-only user who never leaves
the input bar place the marker too.

The widget intentionally has no reference to `QuasselClient`: routing
text to `send_input` lives in the app, so a phase-11 /-command parser
can intercept the message before it hits the wire without having to
modify this widget at all.

Kept as its own widget (rather than using `Input` directly in the
screen) so a future /-command parser has a stable home and so the
app can change the placeholder text per-buffer without reaching
into a foreign widget class.
"""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.widgets import Input

from quasseltui.app.messages import LineSubmitted, MarkerToLatestRequested

# Bounded history so an extremely chatty session can't grow memory
# forever. 100 lines covers any realistic "what did I type earlier"
# recall; irssi defaults to the same order of magnitude.
_HISTORY_LIMIT = 100


class InputBar(Input):
    """Single-line text input docked at the bottom of the chat screen.

    Keeps a per-session history of submitted lines, recalled with
    Up/Down — muscle memory for every IRC user (fix a typo'd line,
    repeat a command). Up from a fresh prompt stashes any in-progress
    text and walks backwards; Down walks forward and restores the
    stash past the newest entry.
    """

    DEFAULT_PLACEHOLDER: ClassVar[str] = "Type a message and press Enter…"
    """Exposed so the app can restore it after the disconnected-state
    placeholder (which names the disconnect reason) on reconnect."""

    # `Input` doesn't use up/down; binding them here doesn't shadow any
    # cursor movement. The app's buffer-cycle bindings are alt+arrow,
    # so plain arrows are free for history.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "history_prev", "Previous input", show=False),
        Binding("down", "history_next", "Next input", show=False),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(
            placeholder=self.DEFAULT_PLACEHOLDER,
            id=id,
        )
        self._history: list[str] = []
        # `None` = not browsing history (live prompt); otherwise the
        # index into `_history` currently shown.
        self._history_index: int | None = None
        # In-progress text stashed when the user starts browsing, so
        # walking past the newest entry gives their draft back.
        self._history_stash: str = ""

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Route an Enter press to the right intent message.

        `event.stop()` prevents the original `Input.Submitted` from
        bubbling into the app's handlers — the app subscribes to our
        narrower messages (`LineSubmitted` for text, or
        `MarkerToLatestRequested` for empty Enter) which are more
        ergonomic to match on.

        Empty Enter is interpreted as "place a read-up-to-here marker
        at the latest message in the active buffer", so a user who
        never leaves the input bar can still advance the marker from
        the keyboard. The app resolves the active buffer and the
        actual `MsgId`; the widget has no state to consult for that.

        Whitespace-only Enter is a no-op that just clears the bar — a
        space + Enter accident must neither broadcast a blank-looking
        message to the channel nor count as a deliberate marker move.

        Non-empty Enter clears `self.value` eagerly to close the
        duplicate-submit window. The app restores on failure.
        """
        event.stop()
        text = event.value
        if not text:
            self.post_message(MarkerToLatestRequested())
            return
        if not text.strip():
            self.value = ""
            return
        self.value = ""
        self._remember(text)
        self.post_message(LineSubmitted(text=text))

    def _remember(self, text: str) -> None:
        if not self._history or self._history[-1] != text:
            self._history.append(text)
            if len(self._history) > _HISTORY_LIMIT:
                del self._history[: len(self._history) - _HISTORY_LIMIT]
        self._history_index = None
        self._history_stash = ""

    def action_history_prev(self) -> None:
        if not self._history:
            return
        if self._history_index is None:
            self._history_stash = self.value
            self._history_index = len(self._history) - 1
        else:
            self._history_index = max(0, self._history_index - 1)
        self.value = self._history[self._history_index]
        self.cursor_position = len(self.value)

    def action_history_next(self) -> None:
        if self._history_index is None:
            return
        self._history_index += 1
        if self._history_index >= len(self._history):
            self._history_index = None
            self.value = self._history_stash
        else:
            self.value = self._history[self._history_index]
        self.cursor_position = len(self.value)


__all__ = [
    "InputBar",
]
