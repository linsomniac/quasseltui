"""Top-level `QuasselApp` — the Textual application class.

Phase 6 responsibility: take a `ClientState`, build a `ChatScreen` from
it, expose `Ctrl+Q` as the quit binding.

Phase 7 adds the live-client wiring. When constructed with a
`QuasselClient`, `on_mount` launches the client's receive loop as a
Textual worker via `ClientBridge`. The bridge translates every
`ClientEvent` into a narrow Textual `Message` (`SessionStarted` /
`BufferListUpdated` / `ActiveBufferUpdated` / `SessionEnded`) that
this app handles by querying the current screen and calling a widget
method — widgets stay dumb and expose a `refresh_from_state` /
`set_active_buffer` surface.

`ClientState` is still accepted as a constructor argument (rather than
built here) so `ui-demo` can hand in a static state without a client.
The `client` kwarg is the live-mode handle: when set, the bridge
worker starts on mount and the app closes the client cleanly on
unmount so the socket doesn't leak when the app quits.

Why handlers live on the app and not on each widget: Textual messages
bubble *up* the DOM toward ancestors, not *down* toward descendants.
A descendant widget that wanted to react to a message posted from the
app would need the message to be routed to it explicitly. Handling
the messages at the app level and then calling widget methods via
`query_one` keeps the flow one-directional (app → widget) and avoids
the fragility of depending on Textual's bubbling order.

Startup-failure handling: the app tracks whether it has seen a
`SessionStarted` message. A `SessionEnded` that arrives *before*
`SessionStarted` is treated as a fatal handshake/auth failure — the
app exits with return code 1 and a sanitized error message, so the
user sees an explanation after the terminal is restored instead of a
blank screen. A `SessionEnded` after `SessionStarted` is a mid-
session drop and is logged but does not auto-quit (phase 11 will
surface it in a status bar and optionally reconnect).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, TypeVar

from textual import on
from textual.app import App
from textual.binding import Binding, BindingType
from textual.css.query import NoMatches
from textual.widget import Widget

from quasseltui.app.bridge import ClientBridge
from quasseltui.app.messages import (
    ActiveBufferUpdated,
    BufferListUpdated,
    SessionEnded,
    SessionStarted,
)
from quasseltui.app.screens.chat import ChatScreen
from quasseltui.app.widgets.buffer_tree import BufferTree
from quasseltui.app.widgets.message_log import MessageLog
from quasseltui.client.state import ClientState
from quasseltui.protocol.usertypes import BufferId
from quasseltui.util.text import sanitize_terminal

if TYPE_CHECKING:
    from quasseltui.client.client import QuasselClient

_log = logging.getLogger(__name__)
_WidgetT = TypeVar("_WidgetT", bound=Widget)


class QuasselApp(App[None]):
    """Textual `App` hosting a single `ChatScreen`.

    Satisfies the `ClientBridge.MessageSink` protocol structurally via
    the `active_buffer_id` instance attribute and Textual's built-in
    `post_message`. The bridge holds a reference to `self` as its
    sink; mypy's structural protocol check accepts that because the
    required attributes/methods are present on the class.
    """

    CSS_PATH = "styles.tcss"
    TITLE = "quasseltui"
    # Textual expects `BINDINGS` to be a class attribute, not an
    # instance attribute, so we annotate with `ClassVar` to satisfy
    # ruff's RUF012 mutable-default lint without fighting the framework
    # contract.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        state: ClientState,
        *,
        client: QuasselClient | None = None,
    ) -> None:
        super().__init__()
        self._state = state
        self._client = client
        # Phase 8 will turn this into a reactive attribute so widgets
        # can watch it directly; phase 7 keeps it as a plain attribute
        # driven explicitly by the bridge and read by the message
        # handlers below.
        self.active_buffer_id: BufferId | None = None
        # Set from the `SessionStarted` handler. Read from
        # `_on_session_ended` to decide whether a disconnect is a
        # fatal startup failure (auth/TLS/handshake) or a mid-session
        # drop. See the module docstring for the policy.
        self._session_opened: bool = False

    def on_mount(self) -> None:
        self.push_screen(ChatScreen(self._state))
        if self._client is not None:
            bridge = ClientBridge(
                events=self._client.events(),
                sink=self,
                state=self._state,
            )
            # `exclusive=True` guarantees that a second mount (which
            # should never happen in practice but would if a test
            # remounted) cancels the previous bridge before starting
            # a new one, so we never have two bridges racing on the
            # same connection.
            self.run_worker(bridge.run(), name="quassel-bridge", exclusive=True)

    async def on_unmount(self) -> None:
        """Close the live client on app teardown.

        Idempotent: `QuasselClient.close` sets an internal flag and
        returns immediately on the second call, so it is safe to call
        here whether or not the bridge worker has already finished.
        """
        if self._client is not None:
            await self._client.close()

    @on(BufferListUpdated)
    def _on_buffer_list_updated(self, _event: BufferListUpdated) -> None:
        """Refresh the buffer sidebar from current state.

        Queries the current screen because messages fired during
        Textual's startup may arrive before the app has switched
        from its default placeholder screen to our `ChatScreen`.
        `NoMatches` is the expected not-yet-mounted signal and we
        quietly skip — a subsequent update will find the widget
        once the screen is in place.
        """
        tree = self._find(BufferTree)
        if tree is None:
            return
        tree.refresh_from_state()

    @on(ActiveBufferUpdated)
    def _on_active_buffer_updated(self, event: ActiveBufferUpdated) -> None:
        log = self._find(MessageLog)
        if log is None:
            return
        if event.buffer_id is not None:
            log.set_active_buffer(event.buffer_id)
        else:
            log.clear()

    def _find(self, widget_type: type[_WidgetT]) -> _WidgetT | None:
        """Query the current screen for a widget, returning None if absent.

        App-level `query_one` only searches the app's own children,
        not pushed screens, so we go through `self.screen`. The
        `self.screen` property itself falls back to the default
        placeholder screen if `push_screen` hasn't run yet — which
        will not have our custom widgets, hence the NoMatches catch.
        """
        try:
            screen = self.screen
        except Exception:
            return None
        try:
            return screen.query_one(widget_type)
        except NoMatches:
            return None

    @on(SessionStarted)
    def _on_session_started(self, _event: SessionStarted) -> None:
        """Latch the "session succeeded at least once" flag.

        The flag is load-bearing for `_on_session_ended`: without it,
        a pre-session fatal disconnect is indistinguishable from a
        mid-session drop and the user gets no explanation.
        """
        self._session_opened = True

    @on(SessionEnded)
    def _on_session_ended(self, event: SessionEnded) -> None:
        """Handle a live client disconnect.

        The `reason` string is sanitized before any terminal-bound
        sink touches it — `SessionEnded` is carrier for core-supplied
        handshake text (e.g. `ClientLoginReject.error_string`), and
        an attacker-controlled reason could contain ANSI CSI / BEL /
        other control bytes that would otherwise rewrite preceding
        terminal output. The sanitized form is also what goes into
        the exit banner on fatal failures.

        Pre-session disconnects (no `SessionStarted` seen yet) are
        treated as fatal: the app exits with return code 1 and the
        sanitized reason as the exit banner, so the user sees an
        explanation after Textual restores the real terminal. Mid-
        session drops (after `SessionStarted`) are non-fatal — we
        log the reason and leave the last state on screen so the
        user can still scroll history; Ctrl+Q is still the exit.
        """
        safe_reason = sanitize_terminal(event.reason)
        _log.warning("session ended: %s", safe_reason)
        if self._client is not None and not self._session_opened:
            self.exit(return_code=1, message=f"quasseltui: {safe_reason}")


__all__ = [
    "QuasselApp",
]
