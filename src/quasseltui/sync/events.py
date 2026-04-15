"""Public client-facing events emitted by the sync layer.

These are the dataclasses that `QuasselClient.events()` yields — the stable
contract consumed by the Textual UI (and any other embedder). Keep them
narrow: a client-facing event describes something the user *cares about*
("a new buffer appeared", "a message arrived"), not every internal state
mutation.

The module lives in `sync/` (not `client/`) so the dispatcher can emit them
without creating a cross-layer import. `quasseltui.client.events` re-exports
the same names so external callers see them under the documented public
namespace.

Design rules for this file:

- Every event is a frozen dataclass.
- Events carry enough information for a reader who has no access to the
  underlying `ClientState` to do something useful — e.g. `MessageReceived`
  carries the whole `IrcMessage`, not a `(buffer_id, msg_id)` key pair.
- `Disconnected` is always the terminal event: when the client hands one
  out, the iterator stops.

We intentionally do NOT collapse `NetworkAdded` / `NetworkUpdated` into one
"state changed" event. A consumer that needs to update a list widget cares
about the append operation; one that needs to refresh a detail pane cares
about the update. Giving both events distinct types lets each handler match
only what it needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from quasseltui.protocol.enums import MessageFlag, MessageType
from quasseltui.protocol.messages import SessionInit
from quasseltui.protocol.usertypes import BufferId, BufferType, IdentityId, MsgId, NetworkId


@dataclass(frozen=True, slots=True)
class SessionOpened:
    """Handshake is done; `session` is the `SessionInit` snapshot.

    This is the first event a consumer sees. After it, expect a burst of
    `BufferAdded` / `NetworkAdded` / `IdentityAdded` events as the client
    walks the session and dispatches it into the object graph.
    """

    session: SessionInit
    peer_features: frozenset[str]


@dataclass(frozen=True, slots=True)
class NetworkAdded:
    network_id: NetworkId
    name: str


@dataclass(frozen=True, slots=True)
class NetworkUpdated:
    """One of a network's scalar properties changed.

    The payload is a coarse string tag (`field_name` = `"name"` |
    `"connection_state"` | `"my_nick"` | `"current_server"`) plus the new
    value, rather than a full diff snapshot — lets a listener decide
    whether it cares about a specific field or wants to re-read the whole
    Network.
    """

    network_id: NetworkId
    field_name: str
    value: Any = field(compare=False)


@dataclass(frozen=True, slots=True)
class NetworkRemoved:
    network_id: NetworkId


@dataclass(frozen=True, slots=True)
class BufferAdded:
    buffer_id: BufferId
    network_id: NetworkId
    name: str
    type: BufferType


@dataclass(frozen=True, slots=True)
class BufferRenamed:
    buffer_id: BufferId
    name: str


@dataclass(frozen=True, slots=True)
class BufferRemoved:
    buffer_id: BufferId


@dataclass(frozen=True, slots=True)
class IrcMessage:
    """One IRC message as the client sees it.

    This is intentionally a narrower surface than the raw
    `quasseltui.protocol.usertypes.Message`: we keep only the fields the UI
    ever needs to render, and we normalize timestamps to a tz-aware
    datetime. Downstream code that needs the raw details can still reach
    into `state.latest_raw_message` if a debug tool demands it.
    """

    msg_id: MsgId
    buffer_id: BufferId
    network_id: NetworkId
    timestamp: datetime
    type: MessageType
    flags: MessageFlag
    sender: str
    sender_prefixes: str
    contents: str


@dataclass(frozen=True, slots=True)
class MessageReceived:
    """One `displayMsg` RpcCall, decoded + narrowed to `IrcMessage`."""

    message: IrcMessage


@dataclass(frozen=True, slots=True)
class IdentityAdded:
    identity_id: IdentityId
    name: str


@dataclass(frozen=True, slots=True)
class ClientDisconnected:
    """Terminal event. After this the client's event iterator stops.

    `reason` is a short human-readable description; `error` is the
    exception that caused the close if there was one. Named
    `ClientDisconnected` (rather than `Disconnected`) to avoid a name
    collision with `quasseltui.protocol.connection.Disconnected` — the
    re-export in `client/events.py` gives callers the shorter alias.
    """

    reason: str
    error: BaseException | None = field(default=None, compare=False)


ClientEvent = (
    SessionOpened
    | NetworkAdded
    | NetworkUpdated
    | NetworkRemoved
    | BufferAdded
    | BufferRenamed
    | BufferRemoved
    | MessageReceived
    | IdentityAdded
    | ClientDisconnected
)


__all__ = [
    "BufferAdded",
    "BufferRemoved",
    "BufferRenamed",
    "ClientDisconnected",
    "ClientEvent",
    "IdentityAdded",
    "IrcMessage",
    "MessageReceived",
    "NetworkAdded",
    "NetworkRemoved",
    "NetworkUpdated",
    "SessionOpened",
]
