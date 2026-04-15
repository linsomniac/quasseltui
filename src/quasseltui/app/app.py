"""Top-level `QuasselApp` — the Textual application class.

Phase 6 responsibility is deliberately tiny: take a `ClientState`,
build a `ChatScreen` from it, and expose `Ctrl+Q` as the quit binding.

Phase 7 will grow this: on `on_mount` it will launch the
`QuasselClient.events()` receive loop as a Textual worker, wire the
bridge that converts `ClientEvent`s into Textual `Message`s, and post
them to the widgets. All of that plumbing routes through this file so
the bridge has a single place to `self.post_message(...)` from.

`ClientState` is accepted as a constructor argument rather than built
here so tests and the `ui-demo` subcommand can hand in whichever state
they want. Phase 7 will add a `from_connection(...)` classmethod or
similar to hide the live-connection wiring.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType

from quasseltui.app.screens.chat import ChatScreen
from quasseltui.client.state import ClientState


class QuasselApp(App[None]):
    """Textual `App` hosting a single `ChatScreen`."""

    CSS_PATH = "styles.tcss"
    TITLE = "quasseltui"
    # Textual expects `BINDINGS` to be a class attribute, not an instance
    # attribute, so we annotate with `ClassVar` to satisfy ruff's RUF012
    # mutable-default lint without fighting the framework contract.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, state: ClientState) -> None:
        super().__init__()
        self._state = state

    def on_mount(self) -> None:
        self.push_screen(ChatScreen(self._state))


__all__ = [
    "QuasselApp",
]
