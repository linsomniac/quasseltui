"""Static placeholder `ClientState` used by `quasseltui ui-demo`.

Phase 6 builds the Textual shell without touching the network — the whole
`quasseltui.client` stack is bypassed and the widgets read from a hand-
built `ClientState`. The point is to get the layout, keybindings, and
widget composition verified before adding the live protocol in phase 7.

Design choices baked in here because they matter for later phases:

- We build real `Network`, `BufferInfo`, and `IrcMessage` instances
  (not ad-hoc tuples) so the widgets are already well-shaped for phase 7.
  When phase 7 swaps `build_demo_state()` for `client.state`, nothing in
  the widgets should need to change.
- A sprinkling of multi-network / multi-buffer / multi-message content
  exercises every branch the widgets care about: a status buffer, a
  busy channel, a quiet channel, and a 1:1 query.
- We never import anything from `textual` here — `demo_data.py` is the
  only L5 module a test might want to call on its own without spinning
  up the App, so keeping it Textual-free keeps those tests cheap.
"""

from __future__ import annotations

import datetime as dt

from quasseltui.client.state import ClientState
from quasseltui.protocol.enums import MessageFlag, MessageType
from quasseltui.protocol.usertypes import (
    BufferId,
    BufferInfo,
    BufferType,
    IdentityId,
    MsgId,
    NetworkId,
)
from quasseltui.sync.events import IrcMessage
from quasseltui.sync.identity import Identity
from quasseltui.sync.network import Network, NetworkConnectionState

_DEMO_EPOCH = dt.datetime(2026, 4, 15, 14, 30, 0, tzinfo=dt.UTC)


def build_demo_state() -> ClientState:
    """Return a populated `ClientState` covering the visible UI surface.

    Three buffers on two networks, one of them carrying a handful of
    messages so the message log has content. No side effects — calling
    this twice returns two independent states.
    """
    state = ClientState()
    state.peer_features = frozenset({"LongTime", "SenderPrefixes"})

    libera = _make_network(
        1, "Libera.Chat", "irc.libera.chat", "seanr", NetworkConnectionState.Initialized
    )
    oftc = _make_network(2, "OFTC", "irc.oftc.net", "seanr", NetworkConnectionState.Initialized)
    state.networks[NetworkId(1)] = libera
    state.networks[NetworkId(2)] = oftc

    state.identities[IdentityId(1)] = _make_identity(1, "default", ["seanr", "seanr_"])

    buffers = [
        _buffer(10, 1, BufferType.Status, ""),
        _buffer(11, 1, BufferType.Channel, "#python"),
        _buffer(12, 1, BufferType.Channel, "#rust"),
        _buffer(13, 1, BufferType.Query, "nickbot"),
        _buffer(20, 2, BufferType.Status, ""),
        _buffer(21, 2, BufferType.Channel, "#debian"),
    ]
    for buf in buffers:
        state.buffers[buf.buffer_id] = buf
        state.messages[buf.buffer_id] = []

    state.messages[BufferId(11)] = _demo_messages_python()
    state.messages[BufferId(12)] = _demo_messages_rust()

    return state


def _make_network(
    network_id: int,
    name: str,
    server: str,
    my_nick: str,
    connection_state: NetworkConnectionState,
) -> Network:
    net = Network(str(network_id))
    net.network_name = name
    net.current_server = server
    net.my_nick = my_nick
    net.connection_state = connection_state
    net.is_connected = connection_state == NetworkConnectionState.Initialized
    return net


def _make_identity(identity_id: int, name: str, nicks: list[str]) -> Identity:
    ident = Identity(str(identity_id))
    ident.identity_name = name
    ident.nicks = list(nicks)
    return ident


def _buffer(buffer_id: int, network_id: int, kind: BufferType, name: str) -> BufferInfo:
    return BufferInfo(
        buffer_id=BufferId(buffer_id),
        network_id=NetworkId(network_id),
        type=kind,
        group_id=0,
        name=name,
    )


def _message(
    *,
    msg_id: int,
    buffer_id: int,
    network_id: int,
    offset_seconds: int,
    sender: str,
    contents: str,
    type: MessageType = MessageType.Plain,
    sender_prefixes: str = "",
) -> IrcMessage:
    return IrcMessage(
        msg_id=MsgId(msg_id),
        buffer_id=BufferId(buffer_id),
        network_id=NetworkId(network_id),
        timestamp=_DEMO_EPOCH + dt.timedelta(seconds=offset_seconds),
        type=type,
        flags=MessageFlag.NONE,
        sender=sender,
        sender_prefixes=sender_prefixes,
        contents=contents,
    )


def _demo_messages_python() -> list[IrcMessage]:
    return [
        _message(
            msg_id=1001,
            buffer_id=11,
            network_id=1,
            offset_seconds=0,
            sender="guido",
            contents="anyone awake on 3.14?",
            sender_prefixes="@",
        ),
        _message(
            msg_id=1002,
            buffer_id=11,
            network_id=1,
            offset_seconds=12,
            sender="raymondh",
            contents="I'm here. what's up?",
            sender_prefixes="+",
        ),
        _message(
            msg_id=1003,
            buffer_id=11,
            network_id=1,
            offset_seconds=24,
            sender="guido",
            contents="PEP 703 landed, wondering if anyone tried it under load",
            sender_prefixes="@",
        ),
        _message(
            msg_id=1004,
            buffer_id=11,
            network_id=1,
            offset_seconds=40,
            sender="seanr",
            contents="I can benchmark tomorrow if you want comparison numbers",
        ),
        _message(
            msg_id=1005,
            buffer_id=11,
            network_id=1,
            offset_seconds=55,
            sender="raymondh",
            contents="nice. post them in the topic when you do",
            sender_prefixes="+",
        ),
    ]


def _demo_messages_rust() -> list[IrcMessage]:
    return [
        _message(
            msg_id=2001,
            buffer_id=12,
            network_id=1,
            offset_seconds=-600,
            sender="graydon",
            contents="ok the async trait work finally merged",
        ),
        _message(
            msg_id=2002,
            buffer_id=12,
            network_id=1,
            offset_seconds=-590,
            sender="seanr",
            contents="huge. does it hit stable in the next cycle?",
        ),
    ]


__all__ = [
    "build_demo_state",
]
