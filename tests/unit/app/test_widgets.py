"""Pure-Python unit tests for the phase 6 widget helpers.

We keep these deliberately Textual-free: every assertion exercises a
pure function (`format_message`, `_buffer_label`, `_short_sender`,
`_safe_label`) that has no `on_mount` dependency on a running app. The
"does it compose inside a real App" smoke test lives in
`test_chat_screen.py` and uses `App.run_test()`.
"""

from __future__ import annotations

import datetime as dt

from rich.text import Text

from quasseltui.app.widgets.buffer_tree import _buffer_label, _buffer_sort_key, _safe_label
from quasseltui.app.widgets.message_log import _short_sender, format_message
from quasseltui.protocol.enums import MessageFlag, MessageType
from quasseltui.protocol.usertypes import (
    BufferId,
    BufferInfo,
    BufferType,
    MsgId,
    NetworkId,
)
from quasseltui.sync.events import IrcMessage


def _buffer(name: str, kind: BufferType) -> BufferInfo:
    return BufferInfo(
        buffer_id=BufferId(1),
        network_id=NetworkId(1),
        type=kind,
        group_id=0,
        name=name,
    )


def _message(
    *,
    sender: str,
    contents: str,
    type: MessageType = MessageType.Plain,
    sender_prefixes: str = "",
) -> IrcMessage:
    return IrcMessage(
        msg_id=MsgId(1),
        buffer_id=BufferId(1),
        network_id=NetworkId(1),
        timestamp=dt.datetime(2026, 4, 15, 12, 34, 56, tzinfo=dt.UTC),
        type=type,
        flags=MessageFlag.NONE,
        sender=sender,
        sender_prefixes=sender_prefixes,
        contents=contents,
    )


class TestBufferLabel:
    def test_status_buffer_has_placeholder_label(self) -> None:
        # Status buffers have an empty `name` on the wire — we must
        # still render something selectable so the user can route the
        # server notices somewhere.
        assert _buffer_label(_buffer("", BufferType.Status)) == "(status)"

    def test_channel_buffer_uses_its_name(self) -> None:
        assert _buffer_label(_buffer("#python", BufferType.Channel)) == "#python"

    def test_unnamed_non_status_buffer_has_placeholder(self) -> None:
        assert _buffer_label(_buffer("", BufferType.Query)) == "(unnamed)"


class TestBufferSortKey:
    def test_status_sorts_before_channel(self) -> None:
        # BufferType.Status is 0x01, Channel is 0x02 — the explicit
        # assertion below is the load-bearing one, the enum values may
        # shift if Quassel reshuffles them.
        status_key = _buffer_sort_key(_buffer("", BufferType.Status))
        channel_key = _buffer_sort_key(_buffer("#python", BufferType.Channel))
        assert status_key < channel_key

    def test_name_comparison_is_case_insensitive(self) -> None:
        upper = _buffer_sort_key(_buffer("#Python", BufferType.Channel))
        lower = _buffer_sort_key(_buffer("#python", BufferType.Channel))
        assert upper == lower


class TestShortSender:
    def test_strips_hostmask(self) -> None:
        assert _short_sender("seanr!sean@example.com") == "seanr"

    def test_bare_nick_passes_through(self) -> None:
        assert _short_sender("seanr") == "seanr"


class TestFormatMessage:
    def test_plain_message_shape(self) -> None:
        msg = _message(sender="seanr!sean@example.com", contents="hello", sender_prefixes="@")
        text = format_message(msg)
        # The timestamp uses local tz, so assert on the non-time parts.
        plain = text.plain
        assert " @seanr: hello" in plain
        # No type prefix for Plain.
        assert "NOTICE" not in plain

    def test_action_message_uses_star_prefix(self) -> None:
        msg = _message(sender="seanr", contents="waves", type=MessageType.Action)
        plain = format_message(msg).plain
        assert "* seanr waves" in plain

    def test_notice_message_has_notice_prefix(self) -> None:
        msg = _message(sender="server.example.com", contents="MOTD", type=MessageType.Notice)
        plain = format_message(msg).plain
        assert "NOTICE" in plain
        assert "server.example.com: MOTD" in plain

    def test_ansi_escape_in_contents_is_escaped(self) -> None:
        """Regression for codex review finding: `rich.text.Text` does NOT
        strip raw ESC bytes at render time, so any sanitization has to
        happen in `format_message` before we build the Text. A hostile
        message body like `\\x1b[31mREDRUM` must land in the output as a
        `\\x1b` literal, never as a raw escape."""
        msg = _message(sender="bad", contents="\x1b[31mREDRUM")
        plain = format_message(msg).plain
        assert "\x1b" not in plain
        assert "\\x1b" in plain
        assert "REDRUM" in plain

    def test_bel_and_backspace_in_sender_are_escaped(self) -> None:
        msg = _message(sender="spoof\x07\x08er", contents="hi")
        plain = format_message(msg).plain
        assert "\x07" not in plain
        assert "\x08" not in plain
        # The escaped form should still show the nick body for debugging.
        assert "spoof" in plain and "er" in plain

    def test_sender_prefixes_are_escaped(self) -> None:
        msg = _message(sender="nick", contents="hi", sender_prefixes="@\x1b")
        plain = format_message(msg).plain
        assert "\x1b" not in plain

    def test_newlines_in_contents_are_escaped(self) -> None:
        # A multi-line contents value would let an attacker forge a line
        # that looks like a fresh message from another sender. Dropping
        # LF/CR to their escape form keeps the line boundary we own.
        msg = _message(sender="eve", contents="line1\nnick!eve@evil: line2")
        plain = format_message(msg).plain
        assert "\n" not in plain
        assert "\\x0a" in plain


class TestSafeLabel:
    def test_plain_label_passes_through(self) -> None:
        # The input string must become the literal plain text of the
        # returned Text — no styling, no markup interpretation.
        label = _safe_label("#python")
        assert isinstance(label, Text)
        assert label.plain == "#python"

    def test_rich_markup_is_not_interpreted(self) -> None:
        """Regression for codex review finding: Textual's `Tree` runs
        `Text.from_markup(...)` over raw `str` labels, so a channel name
        like `"[bold red]spoof[/]"` would get parsed as markup and
        re-styled. Wrapping the string in a `Text(...)` up front
        bypasses that path — the markup becomes the visible text."""
        label = _safe_label("[bold red]spoof[/]")
        assert isinstance(label, Text)
        # The brackets are preserved verbatim in `.plain`.
        assert label.plain == "[bold red]spoof[/]"
        # And no styling was applied to the returned Text.
        assert not label._spans  # type: ignore[attr-defined]

    def test_control_chars_in_label_are_escaped(self) -> None:
        label = _safe_label("evil\x1b[31m")
        assert "\x1b" not in label.plain
        assert "\\x1b" in label.plain

    def test_newline_in_label_is_escaped(self) -> None:
        # Textual's `Tree.process_label` splits on newlines and keeps
        # only the first line — meaning a label with `\n` would silently
        # lose data. Escaping newlines here preserves the whole name.
        label = _safe_label("line1\nline2")
        assert "\n" not in label.plain
        assert "line1" in label.plain
        assert "line2" in label.plain
