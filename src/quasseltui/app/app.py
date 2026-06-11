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
restored. A non-fatal `SessionEnded` enters the disconnected state:
the last state stays on screen so the user can still scroll history,
the input bar is disabled with a placeholder naming the reason, and
Ctrl+R rebuilds the client (via `client_factory`) and bridge over the
same `ClientState` — history survives and the re-seeded session
merges into it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar, Literal, TypeVar

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
    MarkerToLatestRequested,
    ReadMarkerPlaced,
    SessionEnded,
)
from quasseltui.app.screens.chat import ChatScreen
from quasseltui.app.widgets.buffer_tree import BufferTree
from quasseltui.app.widgets.input_bar import InputBar
from quasseltui.app.widgets.message_log import MessageLog
from quasseltui.client.state import ClientState
from quasseltui.protocol.errors import QuasselError
from quasseltui.protocol.usertypes import BufferId, BufferInfo, MsgId, NetworkId
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
        Binding("ctrl+r", "reconnect", "Reconnect", priority=True),
        Binding("alt+up", "prev_buffer", "Previous buffer", priority=True, show=False),
        Binding("alt+down", "next_buffer", "Next buffer", priority=True, show=False),
    ]

    def __init__(
        self,
        state: ClientState,
        *,
        client: QuasselClient | None = None,
        client_factory: Callable[[], QuasselClient] | None = None,
    ) -> None:
        super().__init__()
        self._state = state
        self._client = client
        # Builds a replacement client around the SAME ClientState for
        # Ctrl+R after a mid-session drop. Supplied by the `ui` CLI
        # path (which holds the credentials); absent in `ui-demo` and
        # for embedders that don't want reconnect.
        self._client_factory = client_factory
        # Text the user had typed when the connection died — cleared
        # out of the input bar so the disconnect placeholder is
        # visible (Textual renders placeholders only on an empty
        # value), and restored on reconnect.
        self._pending_input: str | None = None
        # Phase 8 will turn this into a reactive attribute so widgets
        # can watch it directly; phase 7 keeps it as a plain attribute
        # driven explicitly by the bridge and read by the message
        # handlers below.
        self.active_buffer_id: BufferId | None = None
        # Set when a mid-session disconnect lands; gates the
        # disconnected-state surface (disabled input + placeholder)
        # and post-drop backlog requests. Cleared by a successful
        # Ctrl+R `action_reconnect`.
        self.connection_lost: bool = False
        # In-app notice history. A full-screen TUI hides `logging`
        # output, so user-facing warnings (failed sends, lost
        # connection) are recorded here AND surfaced as a toast via
        # `_notify_user`. A future status/log pane can render these.
        self.notices: list[str] = []

    def on_mount(self) -> None:
        self.push_screen(ChatScreen(self._state))
        if self._client is not None:
            self._start_bridge()

    def _start_bridge(self) -> None:
        """Launch the bridge worker over the current client's events.

        `exclusive=True` guarantees that a second start (a remounting
        test, or `action_reconnect` replacing the client) cancels the
        previous bridge before starting a new one, so we never have
        two bridges racing on the same sink.
        """
        assert self._client is not None
        bridge = ClientBridge(
            events=self._client.events(),
            sink=self,
            state=self._state,
        )
        self.run_worker(bridge.run(), name="quassel-bridge", exclusive=True)

    async def action_reconnect(self) -> None:
        """Ctrl+R: rebuild the client and bridge after a mid-session drop.

        Only acts when the connection is actually lost — tearing down a
        healthy session would be a destructive misclick. The replacement
        client shares the existing `ClientState` (history survives); the
        dispatcher's re-seed clears the per-session backlog latches so
        the next refresh re-fetches and merges the gap. Any input text
        stashed at disconnect time is restored.
        """
        if self._client is None or self._client_factory is None:
            return
        if not self.connection_lost:
            self._notify_user("Already connected")
            return
        await self._client.close()  # idempotent; usually already closed
        self._client = self._client_factory()
        self.connection_lost = False
        input_bar = self._find(InputBar)
        if input_bar is not None:
            input_bar.disabled = False
            input_bar.placeholder = InputBar.DEFAULT_PLACEHOLDER
            if self._pending_input and not input_bar.value:
                input_bar.value = self._pending_input
        self._pending_input = None
        self._notify_user("Reconnecting…")
        self._start_bridge()

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
                log.clear_options()
        tree = self._find(BufferTree)
        if tree is not None:
            tree.set_active_buffer(event.buffer_id)
        if event.buffer_id is not None and self._client is not None and not self.connection_lost:
            # Don't chase backlog once the socket is gone — a post-drop
            # buffer switch (alt+up/down still fire) would otherwise spawn
            # requests that fail against the closed client and spam the
            # "Could not load history" notice.
            self.run_worker(self._request_backlog(event.buffer_id), exclusive=False)

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

    @on(ReadMarkerPlaced)
    def _on_read_marker_placed(self, event: ReadMarkerPlaced) -> None:
        """Record a user-placed read marker and refresh the log.

        Writing into `state.read_markers` replaces any prior marker
        for that buffer (dict semantics), which is exactly the "only
        one marker per buffer, always the most recent placement"
        contract the feature asks for. We then re-post
        `ActiveBufferUpdated` so `MessageLog.set_active_buffer`
        rebuilds; because the buffer_id matches the current active
        pointer, the refresh preserves the user's highlighted option
        and scroll position so the cursor stays on the row they just
        marked.
        """
        self._state.read_markers[event.buffer_id] = event.msg_id
        self._sync_marker_to_core(event.buffer_id, event.msg_id)
        if event.buffer_id == self.active_buffer_id:
            self.post_message(ActiveBufferUpdated(buffer_id=event.buffer_id))

    @on(MarkerToLatestRequested)
    def _on_marker_to_latest_requested(self, event: MarkerToLatestRequested) -> None:
        """Drop the read marker on the newest message in the active buffer.

        Fired when the user presses Enter in an empty input bar. The
        widget doesn't know about `MsgId`s; the app resolves "latest"
        against `state.messages[active]` here. Two silent no-op branches:
        no active buffer (the app is still settling after startup),
        or an empty buffer (nothing to mark — and writing an unanchored
        marker would render as a floating separator with no message
        above it once the first message arrives).

        We reuse `ActiveBufferUpdated` for the redraw so the rebuild
        path is the same one `_on_read_marker_placed` uses; that keeps
        a single rebuild route that picks up `state.read_markers` and
        is exercised by both marker-entry tests.
        """
        del event
        buffer_id = self.active_buffer_id
        if buffer_id is None:
            return
        messages = self._state.messages.get(buffer_id)
        if not messages:
            return
        self._state.read_markers[buffer_id] = messages[-1].msg_id
        self._sync_marker_to_core(buffer_id, messages[-1].msg_id)
        self.post_message(ActiveBufferUpdated(buffer_id=buffer_id))

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
            # Tell the user the line didn't go out — without this the
            # restored text just silently reappears, which reads as "I
            # pressed Enter and nothing happened".
            self._notify_user(f"Message not sent: {exc}", severity="warning")

    def _restore_input(self, text: str) -> None:
        """Put `text` back in the input bar after a failed send.

        Only restores if the input bar is still empty — if the user
        has already started typing something new, we don't overwrite
        their work. Silent no-op if the bar is not yet mounted. Once
        the connection is lost the bar is disabled and shows the
        disconnect placeholder, so the text is stashed for the
        reconnect path instead of being trapped in a disabled widget.
        """
        if self.connection_lost:
            if self._pending_input is None:
                self._pending_input = text
            return
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
        previous = self.active_buffer_id
        self.active_buffer_id = buffer_id
        self.post_message(ActiveBufferUpdated(buffer_id=buffer_id))
        if previous is not None and previous != buffer_id:
            self._report_last_seen(previous)

    def _report_last_seen(self, buffer_id: BufferId) -> None:
        """Fire-and-forget requestSetLastSeenMsg for a buffer being left.

        Round-trips read state through the core so reading here clears
        the unread flags in every other Quassel client. Only fires on
        user-driven switches (`_set_active_buffer`); the bridge's
        default-pick writes the pointer directly and must not mark
        anything read on the user's behalf.
        """
        if self._client is None or self.connection_lost:
            return
        messages = self._state.messages.get(buffer_id)
        if not messages:
            return
        self.run_worker(self._send_last_seen(buffer_id, messages[-1].msg_id), exclusive=False)

    async def _send_last_seen(self, buffer_id: BufferId, msg_id: MsgId) -> None:
        if self._client is None:
            return
        try:
            await self._client.set_last_seen(buffer_id, msg_id)
        except QuasselError as exc:
            # Not user-notified: losing one read-state sync is invisible
            # locally and the next switch retries naturally.
            _log.warning("set_last_seen failed for buffer %d: %s", int(buffer_id), exc)

    def _sync_marker_to_core(self, buffer_id: BufferId, msg_id: MsgId) -> None:
        """Fire-and-forget requestSetMarkerLine for a user-placed marker.

        The core persists marker lines, so a marker placed here survives
        restarts (re-seeded from BufferSyncer InitData) and shows up in
        other clients.
        """
        if self._client is None or self.connection_lost:
            return
        self.run_worker(self._send_marker_line(buffer_id, msg_id), exclusive=False)

    async def _send_marker_line(self, buffer_id: BufferId, msg_id: MsgId) -> None:
        if self._client is None:
            return
        try:
            await self._client.set_marker_line(buffer_id, msg_id)
        except QuasselError as exc:
            _log.warning("set_marker_line failed for buffer %d: %s", int(buffer_id), exc)

    async def _request_backlog(self, buffer_id: BufferId) -> None:
        """Fire-and-forget backlog request. Errors are logged, not raised."""
        if self._client is None:
            return
        try:
            await self._client.request_backlog(buffer_id)
        except QuasselError as exc:
            _log.warning("backlog request failed for buffer %d: %s", int(buffer_id), exc)
            self._notify_user(f"Could not load history: {exc}", severity="warning")

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
            return
        # Non-fatal mid-session drop: keep the last state on screen so
        # the user can still scroll history, but make the loss VISIBLE.
        # Before this, the drop only hit the (hidden) warning log, so the
        # app silently went quiet and kept swallowing typed lines — the
        # 2026-06 review's strongest "feels flaky" finding.
        if self._client is not None:
            self._enter_disconnected_state(safe_reason)

    def _enter_disconnected_state(self, reason: str) -> None:
        """Latch the disconnected state and surface it to the user.

        Idempotent — a second `SessionEnded` (or a redraw) won't stack
        notices or re-disable an already-disabled bar. Disabling the
        input bar is the honest signal: a typed line has nowhere to go
        until the user reconnects, and the placeholder names both the
        reason and the remedy. Any text sitting in the bar is stashed
        (Textual only renders the placeholder when the value is empty,
        and the most common way to discover a disconnect is pressing
        Enter on a line that fails to send) and restored on Ctrl+R.
        """
        if self.connection_lost:
            return
        self.connection_lost = True
        self._notify_user(f"Disconnected: {reason}", severity="error")
        input_bar = self._find(InputBar)
        if input_bar is not None:
            input_bar.disabled = True
            if input_bar.value:
                self._pending_input = input_bar.value
                input_bar.value = ""
            input_bar.placeholder = f"Disconnected: {reason} — Ctrl+R to reconnect, Ctrl+Q to quit"

    def _notify_user(
        self,
        message: str,
        *,
        severity: Literal["information", "warning", "error"] = "information",
    ) -> None:
        """Record a user-facing notice and show it as a toast.

        The record in `self.notices` is the durable half (a TUI hides
        `logging`, and toasts auto-dismiss); the toast is the immediate
        half. Both go through here so every user-facing notice has one
        code path.

        The message is sanitized and length-bounded because some notices
        embed untrusted core text (disconnect reasons, exception strings
        wrapping core errors): control bytes are escaped so they can't
        reach the terminal, and `markup=False` stops bracketed text like
        `[Errno 104]` from being parsed as Rich markup (which would
        restyle the toast or raise on a malformed tag).
        """
        safe = _sanitize_and_truncate_reason(message)
        self.notices.append(safe)
        self.notify(safe, severity=severity, markup=False)


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
