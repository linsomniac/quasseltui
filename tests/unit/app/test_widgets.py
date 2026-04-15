"""Pure-Python unit tests for the phase 6 widget helpers.

We keep these deliberately Textual-free: every assertion exercises a
pure function (`format_message`, `_buffer_label`, `_short_sender`) that
has no `on_mount` dependency on a running app. The "does it compose
inside a real App" smoke test lives in `test_chat_screen.py` and uses
`App.run_test()`.
"""

from __future__ import annotations

import datetime as dt

from quasseltui.app.widgets.buffer_tree import _buffer_label, _buffer_sort_key
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
