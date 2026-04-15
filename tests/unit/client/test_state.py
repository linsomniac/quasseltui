"""Unit tests for `quasseltui.client.state.ClientState`.

`ClientState` is mostly a plain dataclass with a handful of helper methods;
these tests pin the helpers so a casual refactor can't quietly change the
semantics the TUI relies on.
"""

from __future__ import annotations

import datetime as dt

from quasseltui.client.state import ClientState
from quasseltui.protocol.enums import MessageFlag, MessageType
from quasseltui.protocol.usertypes import (
    BufferId,
    BufferInfo,
    BufferType,
    MsgId,
    NetworkId,
)
from quasseltui.sync.events import IrcMessage
from quasseltui.sync.network import Network


def _irc_msg(
    *,
    msg_id: int,
    buffer_id: int,
    network_id: int,
    contents: str = "",
) -> IrcMessage:
    return IrcMessage(
        msg_id=MsgId(msg_id),
        buffer_id=BufferId(buffer_id),
        network_id=NetworkId(network_id),
        timestamp=dt.datetime(2026, 4, 14, tzinfo=dt.UTC),
        type=MessageType.Plain,
        flags=MessageFlag.NONE,
        sender="someone",
        sender_prefixes="",
        contents=contents,
    )


def _buffer(buffer_id: int, network_id: int) -> BufferInfo:
    return BufferInfo(
        buffer_id=BufferId(buffer_id),
        network_id=NetworkId(network_id),
        type=BufferType.Channel,
        group_id=0,
        name=f"#chan{buffer_id}",
    )


class TestClientStateDefaults:
    def test_empty_state_has_expected_defaults(self) -> None:
        state = ClientState()
        assert state.session is None
        assert state.peer_features == frozenset()
        assert state.networks == {}
        assert state.buffers == {}
        assert state.messages == {}
        assert state.identities == {}
        assert state.buffer_syncer is None
        assert state.total_message_count() == 0


class TestStateHelpers:
    def test_network_for_buffer_returns_owning_network(self) -> None:
        state = ClientState()
        state.networks[NetworkId(1)] = Network(object_name="1")
        state.buffers[BufferId(10)] = _buffer(10, 1)
        net = state.network_for_buffer(BufferId(10))
        assert net is not None
        assert net.object_name == "1"

    def test_network_for_buffer_returns_none_for_unknown_buffer(self) -> None:
        state = ClientState()
        assert state.network_for_buffer(BufferId(999)) is None

    def test_messages_for_buffer_creates_empty_list(self) -> None:
        state = ClientState()
        msgs = state.messages_for_buffer(BufferId(10))
        assert msgs == []
        msgs.append(_irc_msg(msg_id=1, buffer_id=10, network_id=1, contents="hi"))
        # The returned list IS the canonical store — subsequent lookup
        # sees our append.
        assert state.messages[BufferId(10)] == msgs
        assert state.total_message_count() == 1

    def test_total_message_count_sums_all_buffers(self) -> None:
        state = ClientState()
        state.messages[BufferId(10)] = [
            _irc_msg(msg_id=1, buffer_id=10, network_id=1),
            _irc_msg(msg_id=2, buffer_id=10, network_id=1),
        ]
        state.messages[BufferId(11)] = [
            _irc_msg(msg_id=3, buffer_id=11, network_id=1),
        ]
        assert state.total_message_count() == 3
