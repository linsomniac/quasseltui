"""Bottom-pane input prompt.

On Enter, the current line is posted to the app as a `LineSubmitted`
message and the widget's value is cleared immediately. If the app's
``send_input`` fails, the app restores the text so the user can retry.
Clearing eagerly (rather than waiting for a success callback) closes
the duplicate-submit window that would otherwise exist between two
rapid Enter presses — without this, the same line could be queued
twice before the first ``send_input`` finishes, violating the
single-writer assumption documented in ``connection.py``.

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

from quasseltui.app.messages import LineSubmitted


class InputBar(Input):
    """Single-line text input docked at the bottom of the chat screen."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(
            placeholder="Type a message and press Enter…",
            id=id,
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Post the submitted line to the app as a `LineSubmitted`.

        `event.stop()` prevents the original `Input.Submitted` from
        bubbling into the app's handlers — the app subscribes to our
        `LineSubmitted` instead, which carries a plain `text: str`
        and is ergonomic to match-on. Empty lines are dropped here
        so the app handler never has to guard against a no-op send.

        The widget clears `self.value` eagerly to close the
        duplicate-submit window. The app restores on failure.
        """
        event.stop()
        text = event.value
        if not text:
            return
        self.value = ""
        self.post_message(LineSubmitted(text=text))


__all__ = [
    "InputBar",
]
