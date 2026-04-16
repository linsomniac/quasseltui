"""Top-level `QuasselApp` — the Textual application class.

Phase 6 responsibility: take a `ClientState`, build a `ChatScreen` from
it, expose `Ctrl+Q` as the quit binding.

Phase 7 adds the live-client wiring. When constructed with a
`QuasselClient`, `on_mount` launches the client's receive loop as a
Textual worker via `ClientBridge`. The bridge translates every
`ClientEvent` into a narrow Textual `Message` (`BufferListUpdated` /
`ActiveBufferUpdated` / `SessionEnded`) that this app handles by
querying the current screen and calling a widget method — widgets
stay dumb and expose a `refresh_from_state` / `set_active_buffer`
surface.

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

Startup-failure handling: the bridge stamps `SessionEnded.fatal=True`
on any disconnect it sees before `SessionOpened` — i.e. a failed
handshake, auth reject, TLS error, or anything else that would
otherwise leave the user in a blank Textual screen. The app's
`_on_session_ended` handler reads that flag, sanitizes and truncates
the reason, and exits the app with return code 1 and a visible exit
banner so the user sees an explanation once the real terminal is
restored. A non-fatal `SessionEnded` is just logged — the last state
stays on screen so the user can still scroll history and quit via
Ctrl+Q; phase 11 will surface it in a status bar and optionally feed
a reconnect supervisor.
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
    BufferSelected,
    LineSubmitted,
    SessionEnded,
)
from quasseltui.app.screens.chat import ChatScreen
from quasseltui.app.widgets.buffer_tree import BufferTree
from quasseltui.app.widgets.input_bar import InputBar
from quasseltui.app.widgets.message_log import MessageLog
from quasseltui.client.state import ClientState
from quasseltui.protocol.errors import QuasselError
from quasseltui.protocol.usertypes import BufferId, BufferInfo, NetworkId
from quasseltui.util.text import sanitize_terminal

if TYPE_CHECKING:
    from quasseltui.client.client import QuasselClient

_log = logging.getLogger(__name__)
_WidgetT = TypeVar("_WidgetT", bound=Widget)

# Hard cap on how many characters of a disconnect reason we show /
# log. A hostile or badly-behaving core could return an arbitrarily
# long error string; sanitizing each control byte to its `\xNN`
# escape form multiplies that by up to 4x, so without a cap a
# pathological case could dump tens of kilobytes into stderr and
# the exit banner. 400 fits ~5 lines on an 80-column terminal,
# which is plenty for every well-formed auth/TLS error we've seen.
_MAX_REASON_LEN = 400


def _sanitize_and_truncate_reason(reason: str) -> str:
    """Make a disconnect reason safe and bounded for terminal display.

    Two steps: `sanitize_terminal` escapes ANSI / C0 / C1 control
    bytes so a hostile peer can't inject terminal escapes into the
    warning log or the exit banner; then we cap the length at
    `_MAX_REASON_LEN` with an explicit trailing marker so a runaway
    reason can't flood the terminal.
    """
    cleaned = sanitize_terminal(reason)
    if len(cleaned) <= _MAX_REASON_LEN:
        return cleaned
    return cleaned[:_MAX_REASON_LEN] + "...[truncated]"


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
    #
    # The alt+up/alt+down cycle bindings use `priority=True` so they
    # fire even while the `Input` widget has focus — without priority,
    # Textual's `Input` swallows arrow keys as cursor-move events and
    # the user would have to Tab out of the input before they could
    # switch buffers. Picking alt+arrow rather than plain arrow keeps
    # plain cursor navigation inside the input working as expected.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("alt+up", "prev_buffer", "Previous buffer", priority=True, show=False),
        Binding("alt+down", "next_buffer", "Next buffer", priority=True, show=False),
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
        """Redraw the message log AND sync the tree cursor.

        This is the single "active buffer changed" reaction point —
        both user-driven changes (`_set_active_buffer` via click, Enter,
        alt+up/alt+down) and bridge-driven changes (the default-pick on
        first session event, the removal-recovery re-pick when the
        active buffer is deleted) funnel through `ActiveBufferUpdated`,
        so syncing the tree cursor here keeps the sidebar visual
        consistent with `active_buffer_id` no matter who flipped it.

        Without the tree sync here, the bridge's direct writes to
        `active_buffer_id` in `_maybe_pick_default_active_buffer` and
        `_handle_buffer_removed` would leave the tree cursor pointing
        at a different buffer than the message log is rendering —
        codex-review finding, reliability class.
        """
        log = self._find(MessageLog)
        if log is not None:
            if event.buffer_id is not None:
                log.set_active_buffer(event.buffer_id)
            else:
                log.clear()
        tree = self._find(BufferTree)
        if tree is not None:
            tree.set_active_buffer(event.buffer_id)

    @on(BufferSelected)
    def _on_buffer_selected(self, event: BufferSelected) -> None:
        """Flip to a user-requested buffer.

        The tree posts this on click/Enter; the alt+up/alt+down
        actions post it too, so there is exactly one code path that
        changes `active_buffer_id`. Idempotent — if the incoming
        buffer_id already matches the current active pointer we
        early-return, which breaks the one-round-trip feedback loop
        between `tree.set_active_buffer` → `tree.select_node` →
        `NodeSelected` → `BufferSelected` that the programmatic
        cycle bindings create.
        """
        if event.buffer_id == self.active_buffer_id:
            return
        self._set_active_buffer(event.buffer_id)

    @on(LineSubmitted)
    async def _on_line_submitted(self, event: LineSubmitted) -> None:
        """Forward a typed line to the core via `QuasselClient.send_input`.

        The widget clears itself eagerly when posting `LineSubmitted`
        to close the duplicate-submit window (two rapid Enters
        before the first `send_input` completes). If the send fails,
        we restore the text so the user can retry.

        Three branches:

        1. No client (`ui-demo` mode) — nothing to send to; the
           widget already cleared itself.
        2. No active buffer — rare but possible (the user hit Enter
           before anything landed on screen). Log; nothing to retry
           because we have no idea what buffer the line was for.
        3. `QuasselError` from `send_input` — restore the text into
           the input bar. Typical causes are a racey buffer removal
           and the broken-pipe cases the client-layer wraps into
           `QuasselError` (see `QuasselClient.send_input`).
        """
        if self._client is None:
            return
        if self.active_buffer_id is None:
            _log.debug("dropping input line with no active buffer: %r", event.text)
            return
        try:
            await self._client.send_input(self.active_buffer_id, event.text)
        except QuasselError as exc:
            _log.warning("send_input failed: %s", exc)
            self._restore_input(event.text)

    def _restore_input(self, text: str) -> None:
        """Put `text` back in the input bar after a failed send.

        Only restores if the input bar is still empty — if the user
        has already started typing something new, we don't overwrite
        their work. Silent no-op if the bar is not yet mounted.
        """
        input_bar = self._find(InputBar)
        if input_bar is not None and not input_bar.value:
            input_bar.value = text

    def action_prev_buffer(self) -> None:
        """Cycle backward through the tree's buffer ordering."""
        self._cycle_buffer(-1)

    def action_next_buffer(self) -> None:
        """Cycle forward through the tree's buffer ordering."""
        self._cycle_buffer(1)

    def _cycle_buffer(self, delta: int) -> None:
        """Move `active_buffer_id` to the next/previous buffer.

        The ordering matches `BufferTree._populate` — networks sorted
        by id, then buffers within a network sorted by (type, name).
        Mirrors rather than queries the tree because the tree may not
        be mounted yet (e.g. if the user somehow triggers the binding
        during the transient pre-screen state). `_set_active_buffer`
        will then ask the tree to move its cursor, if one exists.
        """
        ordered = _ordered_buffer_ids(self._state)
        if not ordered:
            return
        if self.active_buffer_id not in ordered:
            self._set_active_buffer(ordered[0])
            return
        idx = ordered.index(self.active_buffer_id)
        target = ordered[(idx + delta) % len(ordered)]
        self._set_active_buffer(target)

    def _set_active_buffer(self, buffer_id: BufferId) -> None:
        """Flip `active_buffer_id` and post an `ActiveBufferUpdated`.

        Used by `_on_buffer_selected` and by the alt+up/alt+down cycle
        actions. The tree and log sync both happen in the
        `_on_active_buffer_updated` handler, so this method is now the
        single place that writes `active_buffer_id` from the user-
        driven path. The bridge still writes the pointer directly for
        its default-pick and removal-recovery paths, but those also
        post `ActiveBufferUpdated`, so the same handler covers them.
        """
        self.active_buffer_id = buffer_id
        self.post_message(ActiveBufferUpdated(buffer_id=buffer_id))

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

    @on(SessionEnded)
    def _on_session_ended(self, event: SessionEnded) -> None:
        """Handle a live client disconnect.

        The reason string is sanitized (to strip terminal escape
        bytes — `SessionEnded` carries core-supplied handshake text
        like `ClientLoginReject.error_string`, which is untrusted)
        *and* length-bounded (to stop a hostile or runaway core
        from flooding stderr or the exit banner). The same safe
        form is used for both the warning log and the exit banner.

        `event.fatal` is the bridge's pre-computed "pre-session
        failure" flag — see `ClientBridge._handle` for the policy.
        When it's true (startup handshake/auth/TLS failure) the app
        exits with return code 1 and the safe reason as the exit
        banner so the user sees an explanation once Textual
        restores the real terminal. When it's false (mid-session
        drop) we only log — the last state stays on screen so the
        user can still scroll history; Ctrl+Q is the exit.
        """
        safe_reason = _sanitize_and_truncate_reason(event.reason)
        _log.warning("session ended: %s", safe_reason)
        if self._client is not None and event.fatal:
            self.exit(return_code=1, message=f"quasseltui: {safe_reason}")


def _ordered_buffer_ids(state: ClientState) -> list[BufferId]:
    """Flatten `state.buffers` into the same order `BufferTree` renders.

    Networks are sorted by id; within a network, buffers are sorted
    by `(type, name.lower())` so status rises above channels and
    channels rise above queries. Mirrors `BufferTree._populate` and
    its `_buffer_sort_key` so alt+up/alt+down cycles match the
    visual order on screen — users would (rightly) find it jarring
    if the cycle order disagreed with the sidebar.
    """
    ordered: list[BufferId] = []
    for network_id in sorted(state.networks, key=int):
        target = NetworkId(int(network_id))
        network_buffers = [buf for buf in state.buffers.values() if buf.network_id == target]
        network_buffers.sort(key=_cycle_sort_key)
        ordered.extend(buf.buffer_id for buf in network_buffers)
    return ordered


def _cycle_sort_key(buf: BufferInfo) -> tuple[int, str]:
    return (buf.type.value, buf.name.lower())


__all__ = [
    "QuasselApp",
]
