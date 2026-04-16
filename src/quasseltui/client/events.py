"""Public `ClientEvent` re-export — the stable API for embedders.

The actual dataclasses live in `quasseltui.sync.events`; this module is a
one-line re-export so external callers import from the documented public
path (`quasseltui.client.events`). Keeping the definitions in `sync/` lets
the dispatcher emit them without creating an upward import.

Aliases:

- `Disconnected` → `sync.events.ClientDisconnected` (short name at the
  public surface; the `Client` prefix exists inside the sync layer only to
  avoid colliding with the lower-level `protocol.connection.Disconnected`).
"""

from quasseltui.sync.events import (
    BacklogReceived,
    BufferAdded,
    BufferRemoved,
    BufferRenamed,
    ClientEvent,
    IdentityAdded,
    IrcMessage,
    MessageReceived,
    NetworkAdded,
    NetworkRemoved,
    NetworkUpdated,
    SessionOpened,
)
from quasseltui.sync.events import (
    ClientDisconnected as Disconnected,
)

__all__ = [
    "BacklogReceived",
    "BufferAdded",
    "BufferRemoved",
    "BufferRenamed",
    "ClientEvent",
    "Disconnected",
    "IdentityAdded",
    "IrcMessage",
    "MessageReceived",
    "NetworkAdded",
    "NetworkRemoved",
    "NetworkUpdated",
    "SessionOpened",
]
