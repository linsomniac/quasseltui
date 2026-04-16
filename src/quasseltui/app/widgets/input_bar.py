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

from textual.widgets import Input

from quasseltui.app.messages import LineSubmitted, MarkerToLatestRequested


class InputBar(Input):
    """Single-line text input docked at the bottom of the chat screen."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(
            placeholder="Type a message and press Enter…",
            id=id,
        )

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

        Non-empty Enter clears `self.value` eagerly to close the
        duplicate-submit window. The app restores on failure.
        """
        event.stop()
        text = event.value
        if not text:
            self.post_message(MarkerToLatestRequested())
            return
        self.value = ""
        self.post_message(LineSubmitted(text=text))


__all__ = [
    "InputBar",
]
