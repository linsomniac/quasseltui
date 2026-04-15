"""Quassel `Network` syncable — one IRC network, owns channels and users.

Mirrors the subset of `src/common/network.{h,cpp}` we need for v1 dump-state
and (eventually) the TUI's sidebar. We track:

- `network_name`, `current_server`, `my_nick`, `connection_state`, `latency`
  as scalar state,
- `channels_by_name` and `users_by_nick` as the rosters the core maintains
  per-network,
- a list of per-channel / per-user object names the dispatcher created on
  our behalf, so a caller can walk the object graph without having to do
  string-key surgery.

The `ircUsersAndChannels` init field carries the entire roster as a nested
`QVariantMap` — we can't create `IrcUser` / `IrcChannel` instances ourselves
here (the dispatcher owns the instance registry), so we store the nested
map verbatim and let the dispatcher seed the child SyncObjects during the
`Network` InitData apply step.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, ClassVar

from quasseltui.sync.base import SyncObject, init_field, sync_slot


class NetworkConnectionState(IntEnum):
    """Mirror of `Network::ConnectionState` in `src/common/network.h`.

    Quassel's connection state machine is a simple progression — we mostly
    care about `Disconnected` vs `Initialized` for dump-state, but keeping
    the full enum means the TUI can colour channels by state later.
    """

    Disconnected = 0
    Connecting = 1
    Initializing = 2
    Initialized = 3
    Reconnecting = 4
    Disconnecting = 5


class Network(SyncObject):
    """Syncable `Network` object, one per configured IRC network.

    The `object_name` is the stringified network id (e.g. `"1"`).
    """

    CLASS_NAME: ClassVar[bytes] = b"Network"

    def __init__(self, object_name: str) -> None:
        super().__init__(object_name)
        self.network_name: str = ""
        self.current_server: str = ""
        self.my_nick: str = ""
        self.latency: int = 0
        self.connection_state: NetworkConnectionState = NetworkConnectionState.Disconnected
        self.is_connected: bool = False
        # Populated from `ircUsersAndChannels` init field. Keys are C++
        # object names without the network prefix — `"#python"` for a
        # channel, `"seanr"` for a user. Values are the nested QVariantMap
        # the core shipped us, verbatim.
        self.channels_seed: dict[str, dict[str, Any]] = {}
        self.users_seed: dict[str, dict[str, Any]] = {}
        # Runtime rosters maintained by slot handlers. These hold names/
        # nicks; the dispatcher owns the actual IrcChannel/IrcUser instances
        # by (className, objectName) key.
        self.channels: set[str] = set()
        self.users: set[str] = set()

    @property
    def network_id(self) -> int:
        """Parse the object name back into an int network id.

        The object_name is always set at construction time by the
        dispatcher from a `NetworkId` in `SessionInit`, so this should
        never actually fail in practice — but we return `-1` rather than
        raising if it somehow does, to avoid breaking a dump-state print
        over a stringly-typed field.
        """
        try:
            return int(self.object_name)
        except ValueError:
            return -1

    # -- slot handlers ------------------------------------------------------

    @sync_slot(b"setNetworkName")
    def _sync_set_network_name(self, name: str) -> None:
        self.network_name = str(name)

    @sync_slot(b"setCurrentServer")
    def _sync_set_current_server(self, server: str) -> None:
        self.current_server = str(server)

    @sync_slot(b"setMyNick")
    def _sync_set_my_nick(self, nick: str) -> None:
        self.my_nick = str(nick)

    @sync_slot(b"setLatency")
    def _sync_set_latency(self, latency: int) -> None:
        self.latency = int(latency)

    @sync_slot(b"setConnected")
    def _sync_set_connected(self, connected: bool) -> None:
        self.is_connected = bool(connected)

    @sync_slot(b"setConnectionState")
    def _sync_set_connection_state(self, state: int) -> None:
        self.connection_state = _coerce_connection_state(state)

    @sync_slot(b"addIrcUser")
    def _sync_add_irc_user(self, hostmask: str) -> None:
        # Hostmask is `nick!user@host`; the core indexes by the nick part.
        nick = str(hostmask).split("!", 1)[0]
        if nick:
            self.users.add(nick)

    @sync_slot(b"addIrcChannel")
    def _sync_add_irc_channel(self, name: str) -> None:
        if name:
            self.channels.add(str(name))

    # -- init-field handlers ------------------------------------------------

    @init_field("networkName")
    def _init_network_name(self, value: Any) -> None:
        self.network_name = str(value) if value is not None else ""

    @init_field("currentServer")
    def _init_current_server(self, value: Any) -> None:
        self.current_server = str(value) if value is not None else ""

    @init_field("myNick")
    def _init_my_nick(self, value: Any) -> None:
        self.my_nick = str(value) if value is not None else ""

    @init_field("latency")
    def _init_latency(self, value: Any) -> None:
        self.latency = int(value) if value is not None else 0

    @init_field("isConnected")
    def _init_is_connected(self, value: Any) -> None:
        self.is_connected = bool(value)

    @init_field("connectionState")
    def _init_connection_state(self, value: Any) -> None:
        self.connection_state = _coerce_connection_state(value)

    @init_field("IrcUsersAndChannels")
    def _init_irc_users_and_channels(self, value: Any) -> None:
        """Capture the nested user+channel seed map for the dispatcher.

        Quassel ships this as `{"Users": {obj_name: {...fields}}, "Channels":
        {obj_name: {...}}}` — the wire format is a `QVariantMap` containing
        two nested maps. The dispatcher will pull these out and create the
        corresponding `IrcUser` / `IrcChannel` SyncObjects, because *we*
        don't know the other classes' object-name conventions.
        """
        if not isinstance(value, dict):
            return
        users = value.get("Users") or value.get("users")
        channels = value.get("Channels") or value.get("channels")
        if isinstance(users, dict):
            # Each value may itself be a QVariantMap of parallel lists or a
            # ready-made per-user dict depending on core version; we store
            # it as-is for the dispatcher to unpack.
            self.users_seed = {str(k): dict(v) for k, v in users.items() if isinstance(v, dict)}
            self.users.update(self.users_seed.keys())
        if isinstance(channels, dict):
            self.channels_seed = {
                str(k): dict(v) for k, v in channels.items() if isinstance(v, dict)
            }
            self.channels.update(self.channels_seed.keys())


def _coerce_connection_state(value: Any) -> NetworkConnectionState:
    """Try hard to turn a wire value into a `NetworkConnectionState`.

    The core sends this as a qint32 int, but defensive parsing means we
    accept either an int or a `NetworkConnectionState` already. Unknown
    values degrade to `Disconnected` rather than raising — it's a forward-
    compat hedge for a hypothetical new state the plan doesn't anticipate.
    """
    if isinstance(value, NetworkConnectionState):
        return value
    try:
        return NetworkConnectionState(int(value))
    except (TypeError, ValueError):
        return NetworkConnectionState.Disconnected


__all__ = [
    "Network",
    "NetworkConnectionState",
]
