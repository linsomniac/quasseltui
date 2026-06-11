"""Center-pane scrollback for the active buffer.

Built on Textual's `OptionList` rather than `RichLog` so each message
row is individually addressable: the user can Tab into the log, walk
the cursor with arrow keys, and press Enter to place a "read up to
here" marker. The marker lives in `ClientState.read_markers` keyed by
`BufferId`; the rebuild path here reads it out and inserts a disabled
option row at the matching message's position so the marker is
rendered inline with the message stream.

Why `OptionList` over `RichLog`:

- `RichLog` is a pure scrollback with no concept of a "current line" —
  it can't give us the per-row focus that the marker UX needs.
- `ListView` would technically work but materializes one Textual
  `Widget` per row, which is fine for a settings screen but not for
  a 5 000-message channel scrollback.
- `OptionList` renders options lazily via Rich strips (same performance
  class as `RichLog`) while giving us `highlighted`, arrow-key
  navigation, and `OptionSelected` on Enter — exactly what we need.

Rebuild rather than incremental update: when a live message arrives
the bridge debounces and posts `ActiveBufferUpdated`, and the handler
calls `set_active_buffer(active)` here. The method tears the option
list down and rebuilds from `state.messages[buffer_id]`. A per-buffer
rebuild is O(n) in messages and n is bounded by
`max_messages_per_buffer` (5 000 by default), so a full rebuild on the
trailing edge of the 50 ms debounce window is cheap compared to the
incremental-update bookkeeping an append path would need (especially
once we also have to keep the marker row in sync). We preserve the
user's highlighted option across same-buffer rebuilds by stashing its
id before `clear_options` and re-highlighting after; on a true buffer
switch we drop the highlight and scroll to the tail.

Focus UX: the screen's `AUTO_FOCUS` still points at the input bar, so
typing is the default action. The user Tabs into the log when they
want to drop a marker; Up/Down moves the cursor, Enter places the
marker, Shift+Tab or Tab cycles back out. We don't bind Escape to
return focus — Textual's built-in Tab cycling is enough and adding an
Escape would fight the `Input` widget's own Escape semantics.
"""

from __future__ import annotations

import datetime as _dt

from rich.text import Text
from textual import events
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from quasseltui.app.messages import ReadMarkerPlaced
from quasseltui.client.state import ClientState
from quasseltui.protocol.enums import MessageType
from quasseltui.protocol.usertypes import BufferId, MsgId
from quasseltui.sync.events import IrcMessage
from quasseltui.util.text import sanitize_terminal, strip_mirc_formatting

_TYPE_PREFIX: dict[MessageType, str] = {
    MessageType.Plain: "",
    MessageType.Notice: "NOTICE ",
    MessageType.Action: "* ",
    MessageType.Join: "--> ",
    MessageType.Part: "<-- ",
    MessageType.Quit: "<-- ",
    MessageType.Kick: "<-- ",
    MessageType.Nick: "-- ",
    MessageType.Mode: "-- ",
    MessageType.Topic: "-- ",
    MessageType.Server: "!! ",
    MessageType.Error: "!! ",
    MessageType.Info: "-- ",
}

# AIDEV-NOTE: option-id schema. Messages use `msg:<MsgId>`; the marker
# row uses the fixed id below. The app's read-marker handler inspects
# the prefix to decide whether the Enter press targeted a message
# (place a marker) or a non-message row (ignore). Keep these constants
# in sync with `MessageLog._message_option_id` / `_marker_option_id`.
_MESSAGE_OPTION_PREFIX = "msg:"
_MARKER_OPTION_ID = "read-marker"

# Characters around the marker text. Em-dashes render cleanly on every
# terminal we target and visually differentiate the marker row from a
# chat line (which always starts with `HH:MM:SS`). Kept as a module-level
# constant so a future theme pass can swap it without reaching into the
# render path.
_MARKER_LEFT = "── "
_MARKER_RIGHT = " ──"
_MARKER_TEXT = "read up to here"


class MessageLog(OptionList):
    """Per-buffer scrollback — one option per message, plus a marker row."""

    # Textual reserves arrow keys while an `OptionList` has focus (via
    # the parent class's BINDINGS). The app-level alt+up/alt+down
    # cycle bindings carry `priority=True` so they fire through these
    # regardless of focus. No extra bindings are needed here: Enter is
    # already wired to `action_select` which posts `OptionSelected`,
    # which we handle below.

    def __init__(self, state: ClientState, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._state = state
        self._active_buffer: BufferId | None = None

    def on_mount(self) -> None:
        """Pick an initial buffer so the user sees content on first paint.

        The bridge takes over picking buffers once the live session is
        running, but during `ui-demo` (no client) and in the split-second
        before the first event arrives we want the log to already have
        something in it. Matches the previous behaviour of the `RichLog`-
        backed version.
        """
        active = self._pick_initial_buffer()
        if active is not None:
            self.set_active_buffer(active)

    def set_active_buffer(self, buffer_id: BufferId) -> None:
        """Render `buffer_id`'s history into the option list.

        Two call patterns collapse through this method:

        1. The user switched buffers (`buffer_id` differs from the
           current active). Drop any highlight, scroll to the tail —
           the user expects a "fresh" log showing latest activity.
        2. New messages arrived for the current buffer (bridge's
           debounced `ActiveBufferUpdated`). Preserve the user's
           highlighted row and scroll position so navigating to a
           message and waiting doesn't snatch the cursor back to the
           bottom when someone else speaks.

        The distinction is made by comparing `buffer_id` to
        `self._active_buffer` before the rebuild. We also remember
        whether the scroll was at the tail: if it was, new messages
        should still push the view forward; if the user had scrolled up
        to read history, we anchor on the message that was at the top of
        the viewport so the rebuild doesn't yank them away — `_rebuild`'s
        `clear_options` resets the scroll to the top, and both live
        appends (below the fold) and backlog prepends (above the fold)
        would otherwise dump the reader somewhere they didn't ask to be.
        """
        is_refresh = buffer_id == self._active_buffer
        preserved_id: str | None = None
        was_at_tail = True
        anchor: tuple[str, int] | None = None
        if is_refresh:
            preserved_id = self._current_highlighted_id()
            was_at_tail = self.is_vertical_scroll_end
            if not was_at_tail:
                anchor = self._capture_scroll_anchor()
        self._active_buffer = buffer_id
        self._rebuild()
        if is_refresh and preserved_id is not None:
            self._restore_highlight(preserved_id)
        else:
            self.highlighted = None
        # Exactly one scroll decision so the highlight restore and the
        # scroll restore can't fight each other: a fresh switch or an
        # at-tail refresh follows the newest message; a scrolled-up
        # refresh re-pins the previously-top message in place.
        if not is_refresh or was_at_tail:
            self.scroll_end(animate=False, immediate=True)
        elif anchor is not None:
            self._restore_scroll_anchor(anchor)
        else:
            # Anchor capture unavailable (the private Textual line cache
            # the anchor leans on changed shape, or there was nothing to
            # anchor). `_rebuild`'s clear_options has already reset the
            # scroll to 0, so doing nothing here strands the reader at
            # the TOP of the buffer — the tail is the honest degraded
            # behavior, matching a fresh buffer switch.
            self.scroll_end(animate=False, immediate=True)

    def on_focus(self) -> None:
        """Pre-select the latest message when focus arrives with no highlight.

        Without this, Tabbing into the log leaves `OptionList.highlighted`
        at `None` — the widget shows a focus border but no cursor row,
        and `OptionList.action_select` on Enter silently returns because
        it requires a highlighted option. The user's perception is that
        they pressed Enter "on a blank message" and nothing happened.

        When the user is at the tail we snap to the last non-disabled
        option (the newest message) so Enter immediately drops a marker
        on the most recent line. When they've scrolled up to read
        history we instead land on the top-visible message: still a
        selectable cursor, but it doesn't scroll the view — snapping to
        the tail here would rip the reader off the history they were
        looking at (2026-06 review). If the user has already moved the
        cursor on a prior visit we leave it where it was — preserving
        their navigation state across Tab-in/Tab-out cycles is the whole
        reason we don't reset `highlighted` on blur.
        """
        if self.highlighted is not None:
            return
        # `set_reactive` rather than plain assignment: the reactive
        # watcher scrolls the highlighted option fully into view, and
        # when the top-visible row is partially cut off (wrapped IRC
        # lines) that shifts the viewport the docstring promises not to
        # move. Same precedent as `_restore_highlight`. Focus triggers
        # a render anyway, so the cursor still paints.
        if self.is_vertical_scroll_end:
            self.set_reactive(OptionList.highlighted, self._last_message_index())
        else:
            self.set_reactive(OptionList.highlighted, self._top_visible_message_index())

    async def _on_click(self, event: events.Click) -> None:
        """Clicks highlight a row but do NOT select it.

        AIDEV-NOTE: this overrides OptionList's private `_on_click`,
        which sets `highlighted` and then calls `action_select()` for
        every left click — turning focus clicks and failed
        text-selection attempts into silent read-marker moves (the
        marker is the user's record of where they stopped reading;
        destroying it on a stray click is data loss). Marker placement
        stays on Enter. Same private-API dependency class as the
        scroll-anchor helpers below; if Textual renames `_on_click`,
        the base behavior returns (annoying but functional) rather
        than anything crashing.
        """
        clicked_option: int | None = event.style.meta.get("option")
        if clicked_option is None:
            return
        try:
            disabled = self.get_option_at_index(clicked_option).disabled
        except Exception:
            return  # stale meta during a rebuild — nothing to land on
        if not disabled:
            self.highlighted = clicked_option

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle Enter / click on an option row.

        Enter on a message row: post `ReadMarkerPlaced` so the app can
        update `state.read_markers` and trigger a redraw. The marker
        row itself is created with `disabled=True` so `OptionList`
        never fires `OptionSelected` for it, but we still guard
        defensively in case that invariant changes.
        """
        event.stop()
        if self._active_buffer is None:
            return
        option_id = event.option_id
        if option_id is None or not option_id.startswith(_MESSAGE_OPTION_PREFIX):
            return
        try:
            raw = int(option_id[len(_MESSAGE_OPTION_PREFIX) :])
        except ValueError:
            return
        self.post_message(ReadMarkerPlaced(buffer_id=self._active_buffer, msg_id=MsgId(raw)))

    # -- internals ---------------------------------------------------------

    def _rebuild(self) -> None:
        """Tear down and recreate all options for the active buffer.

        Inserts a disabled date-separator row whenever the (local) date
        changes between consecutive messages, and before the first
        message when it isn't from today — a quiet buffer's 100-message
        backlog can span weeks, and `HH:MM:SS`-only timestamps made all
        of it look like today's traffic. Seeding `previous_date` with
        today means today's messages get no leading separator.
        """
        self.clear_options()
        if self._active_buffer is None:
            return
        messages = self._state.messages.get(self._active_buffer, [])
        marker_msg_id = self._state.read_markers.get(self._active_buffer)
        options: list[Option] = []
        previous_date = _dt.datetime.now().astimezone().date()
        for msg in messages:
            local_date = msg.timestamp.astimezone().date()
            if local_date != previous_date:
                options.append(_date_separator_option(local_date))
                previous_date = local_date
            options.append(
                Option(
                    format_message(msg),
                    id=_message_option_id(msg.msg_id),
                )
            )
            if marker_msg_id is not None and int(msg.msg_id) == int(marker_msg_id):
                options.append(_marker_option())
        if options:
            self.add_options(options)

    def _last_message_index(self) -> int | None:
        """Return the index of the last selectable (message) option.

        Walks from the tail so we skip the marker row in the common
        case where it's the very last option (user previously placed
        a marker on the most recent message). Returns `None` if there
        are no options, or if every option is disabled — either way,
        there is nothing meaningful to land the cursor on.
        """
        for i in range(self.option_count - 1, -1, -1):
            if not self.get_option_at_index(i).disabled:
                return i
        return None

    def _current_highlighted_id(self) -> str | None:
        """Return the id of the currently highlighted option, if any.

        `OptionList.highlighted` is an index (or `None`). We convert to
        the option's `id` so we can re-seek after a rebuild where the
        index has shifted (e.g. one new message was appended so every
        later index moved by one, or the marker was inserted in the
        middle pushing later options down).
        """
        idx = self.highlighted
        if idx is None:
            return None
        try:
            return self.get_option_at_index(idx).id
        except Exception:
            return None

    def _restore_highlight(self, option_id: str) -> None:
        """Put the cursor back on the option with `option_id`, if it still exists.

        Uses `set_reactive` rather than a plain assignment so restoring
        the highlight does NOT trigger OptionList's scroll-to-highlight
        watcher — otherwise it would fight the explicit scroll decision
        in `set_active_buffer` (the cursor would tug the viewport one
        way while the anchor/tail logic pulls it the other, a visible
        jitter). The list was just fully rebuilt, so the new highlight
        value is picked up on the next render regardless.

        Silently no-ops when the option is gone (e.g. the message it
        pointed at was trimmed by the retention cap during the rebuild
        window). The `get_option_index` API raises `OptionDoesNotExist`
        in that case, which we swallow because there's nothing sensible
        to do — leaving `highlighted=None` matches the "fresh-looking
        list after a buffer switch" behaviour.
        """
        try:
            self.set_reactive(OptionList.highlighted, self.get_option_index(option_id))
        except Exception:
            self.set_reactive(OptionList.highlighted, None)

    # AIDEV-NOTE: the scroll-anchor scheme leans on two private Textual
    # OptionList caches — `_lines` (a per-rendered-line list of
    # `(option_index, line_within_option)`) and `_index_to_line` (an
    # option_index -> first-rendered-line map). We need them because IRC
    # lines wrap: one option can span several scroll lines, so
    # `scroll_y` is NOT an option index. These are private APIs; every
    # access goes through the guarded helpers below and degrades to "no
    # anchor" (rather than crashing) if a future Textual renames them.
    # `_index_to_line` is refreshed synchronously by `add_options`, so
    # the post-rebuild restore reads correct line numbers.

    def _scroll_line_to_option(self, line: int) -> int | None:
        """Option index occupying rendered scroll `line`, or None.

        Reads Textual's `_lines` cache so wrapped multi-line options map
        correctly. Clamps `line` into range so a stale `scroll_y` during
        a rebuild can't index past the ends.
        """
        lines = getattr(self, "_lines", None)
        if not lines:
            return None
        line = max(0, min(len(lines) - 1, line))
        try:
            return int(lines[line][0])
        except (IndexError, TypeError, ValueError):
            return None

    def _option_first_line(self, index: int) -> int | None:
        """First rendered scroll line of an option, via `_index_to_line`."""
        mapping = getattr(self, "_index_to_line", None)
        if mapping is None:
            return None
        value = mapping.get(index)
        return int(value) if value is not None else None

    def _first_message_index_from(self, start: int) -> int | None:
        """First selectable (message) option at or below `start`.

        Skips the disabled marker row so the anchor is always a stable
        message id — anchoring on the marker would let a marker move drag
        the viewport. Falls back upward if everything below is disabled.
        """
        start = max(0, min(self.option_count - 1, start))
        for i in range(start, self.option_count):
            if not self.get_option_at_index(i).disabled:
                return i
        for i in range(start - 1, -1, -1):
            if not self.get_option_at_index(i).disabled:
                return i
        return None

    def _capture_scroll_anchor(self) -> tuple[str, int] | None:
        """Snapshot the viewport: a stable message id + line offset from its top.

        Anchors on the first *message* option intersecting the top of the
        view (skipping the marker row) and records how many rendered lines
        the viewport top sits below that option's first line, so the
        restore can reproduce the exact position even when rows wrap.
        Returns None when there's nothing to anchor on.
        """
        if self.option_count == 0:
            return None
        top_line = round(self.scroll_y)
        top_index = self._scroll_line_to_option(top_line)
        if top_index is None:
            return None
        anchor_index = self._first_message_index_from(top_index)
        if anchor_index is None:
            return None
        option_id = self.get_option_at_index(anchor_index).id
        if option_id is None:
            return None
        first_line = self._option_first_line(anchor_index)
        if first_line is None:
            return None
        return (option_id, top_line - first_line)

    def _restore_scroll_anchor(self, anchor: tuple[str, int]) -> None:
        """Scroll the anchored message back to its captured viewport position.

        Live appends leave the message's first line unchanged (reader
        doesn't move); backlog prepends push it down by the fetched rows'
        height (same content stays in view with history added above).
        No-op if the anchor was evicted by the retention cap during the
        rebuild or the line cache is unavailable.
        """
        option_id, delta = anchor
        try:
            index = self.get_option_index(option_id)
        except Exception:
            return
        first_line = self._option_first_line(index)
        if first_line is None:
            return
        self.scroll_to(y=max(0, first_line + delta), animate=False, immediate=True)

    def _top_visible_message_index(self) -> int | None:
        """First selectable message option at the top of the view.

        Used by `on_focus` to land the cursor on a visible row without
        scrolling. Goes through the line cache so it's correct when rows
        wrap.
        """
        if self.option_count == 0:
            return None
        top_index = self._scroll_line_to_option(round(self.scroll_y))
        if top_index is None:
            return None
        return self._first_message_index_from(top_index)

    def _pick_initial_buffer(self) -> BufferId | None:
        """Default-select the first buffer that has any history.

        Mirrors `bridge._pick_default_buffer`: prefer a buffer with
        content so the demo and cold-start cases show something
        immediately, fall back to any buffer if none have content yet.
        The bridge takes over once live events start arriving; this
        method only ever runs in the pre-event gap.
        """
        for buffer_id, messages in self._state.messages.items():
            if messages:
                return buffer_id
        return next(iter(self._state.buffers), None)


def format_message(msg: IrcMessage) -> Text:
    """Render one `IrcMessage` as a `rich.text.Text` line.

    Shape: `HH:MM:SS prefix<sender> contents` for Plain/Notice, with a
    type-specific prefix for the non-chat event types. The output is a
    `Text` (not a raw str) so phase 11 can colour senders via
    `Text.stylize` without rewriting the formatter.

    Every user- or core-provided string (sender, sender_prefixes,
    contents) is passed through `sanitize_terminal` first. A
    `rich.text.Text` built from a plain string does NOT strip embedded
    ANSI/C0/C1 bytes at render time — the bytes pass straight through
    to the terminal driver — so the onus is on us to remove them here.
    The type prefix and timestamp are compile-time ASCII and do not
    need sanitizing.
    """
    ts = msg.timestamp.astimezone().strftime("%H:%M:%S")
    prefix = _TYPE_PREFIX.get(msg.type, "")
    sender = sanitize_terminal(_short_sender(msg.sender))
    # mIRC formatting codes (bold/color/...) are stripped rather than
    # escaped — they're routine in real traffic and would otherwise
    # render as literal \x02 junk. The sanitizer stays as the net for
    # everything that isn't benign formatting.
    contents = sanitize_terminal(strip_mirc_formatting(msg.contents))
    if msg.type == MessageType.Action:
        return Text(f"{ts} {prefix}{sender} {contents}")
    sender_prefixes = sanitize_terminal(msg.sender_prefixes)
    return Text(f"{ts} {prefix}{sender_prefixes}{sender}: {contents}")


def _short_sender(sender: str) -> str:
    """Strip the `!user@host` off an IRC hostmask, if present.

    Quassel ships `displayMsg` senders as full hostmasks; we keep the
    nick portion only for the UI, leaving the full string in
    `state.messages` for anything that needs to reason about user@host.
    """
    return sender.split("!", 1)[0] if "!" in sender else sender


def _message_option_id(msg_id: MsgId) -> str:
    """Build the option id for a message row.

    Prefixed so the handler can discriminate message rows from the
    marker row (and any future non-message rows) at dispatch time
    without a separate "which kind of option is this" lookup.
    """
    return f"{_MESSAGE_OPTION_PREFIX}{int(msg_id)}"


def _date_separator_option(day: _dt.date) -> Option:
    """Build a disabled "day changed" separator row.

    `id=None` (several can exist per buffer) and `disabled=True` so the
    cursor, the scroll anchor, and Enter all skip it — the same row
    contract as the read marker.
    """
    text = Text(f"── {day.strftime('%A, %Y-%m-%d')} ──", style="dim")
    return Option(text, id=None, disabled=True)


def _marker_option() -> Option:
    """Build the "read up to here" marker option row.

    Styled inline on the `Text` object rather than via OptionList CSS
    because OptionList applies its `--option-disabled` colour over the
    top of whatever we put in the prompt — embedding the style in the
    Rich `Text` wins the layered-style race so the marker keeps its
    intended colour whether the row is enabled, disabled, hovered, or
    highlighted. `disabled=True` ensures it can't be highlighted or
    selected, so the user can't accidentally land on it with the
    arrow keys or click.
    """
    text = Text(f"{_MARKER_LEFT}{_MARKER_TEXT}{_MARKER_RIGHT}", style="bold yellow")
    return Option(text, id=_MARKER_OPTION_ID, disabled=True)


__all__ = [
    "MessageLog",
    "format_message",
]
