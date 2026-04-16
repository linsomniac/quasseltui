"""Bottom-pane input prompt.

On Enter, the current line is posted to the app as a `LineSubmitted`
message. The widget intentionally has no reference to `QuasselClient`:
routing text to `send_input` lives in the app, so a phase-11 /-command
parser can intercept the message before it hits the wire without
having to modify this widget at all.

**We do not clear the widget on submit.** The app layer clears us
only after a successful `send_input` — if the send fails (the socket
just died, the buffer vanished under us) the typed text stays in the
box so the user can retry on reconnect instead of losing their
message to a log line the alt-screen may have hidden.

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

        The widget deliberately does NOT clear `self.value` here —
        the app clears us after a successful send. See the module
        docstring for why.
        """
        event.stop()
        text = event.value
        if not text:
            return
        self.post_message(LineSubmitted(text=text))


__all__ = [
    "InputBar",
]
