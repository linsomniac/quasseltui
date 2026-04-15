"""L3 — Syncable object model (Network, IrcChannel, IrcUser, ...).

The sync layer consumes the CONNECTED-state `Sync` / `InitData` / `RpcCall`
stream from the protocol layer and maps it onto typed Python objects
(`Network`, `IrcChannel`, `IrcUser`, `Identity`, `BufferSyncer`). The
central piece is `Dispatcher`, which routes inbound frames by
`(class_name, object_name)` to the matching `SyncObject` instance and emits
a flat stream of `ClientEvent`s for higher layers to consume.

This module exposes the common handles but doesn't otherwise run code on
import. Callers usually want to import from `client/` instead — the sync
types are implementation detail for the dispatcher.
"""

from quasseltui.sync.base import SyncObject, init_field, sync_slot
from quasseltui.sync.buffer_syncer import BufferSyncer
from quasseltui.sync.dispatcher import Dispatcher
from quasseltui.sync.identity import Identity
from quasseltui.sync.irc_channel import IrcChannel
from quasseltui.sync.irc_user import IrcUser
from quasseltui.sync.network import Network, NetworkConnectionState

__all__ = [
    "BufferSyncer",
    "Dispatcher",
    "Identity",
    "IrcChannel",
    "IrcUser",
    "Network",
    "NetworkConnectionState",
    "SyncObject",
    "init_field",
    "sync_slot",
]
