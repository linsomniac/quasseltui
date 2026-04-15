"""Bottom-pane input prompt.

Phase 6: visual placeholder — the user can type and the text is
captured, but nothing is sent anywhere. Phase 9 wires the Enter key
to `QuasselClient.send_input()`.

Kept as its own widget (rather than using `Input` directly in the
screen) so phase 11's `/command` parsing has a clear home: the
per-message parsing of `/join`, `/msg`, `/me`, `/quit` is routing
logic that belongs to the input widget, not the chat screen.
"""

from __future__ import annotations

from textual.widgets import Input


class InputBar(Input):
    """Single-line text input docked at the bottom of the chat screen."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(
            placeholder="Type a message and press Enter…",
            id=id,
        )


__all__ = [
    "InputBar",
]
