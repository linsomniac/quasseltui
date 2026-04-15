"""Quassel `IrcUser` syncable — one IRC user visible to a network.

Object-name convention: `"<networkId>/<nick>"`. The dispatcher creates an
`IrcUser` when it first sees the nick in a `Network::addIrcUser` slot call or
in the `Network.IrcUsersAndChannels` init seed.

For phase 5 we only model the fields dump-state needs: nick/user/host,
realName, away status, and the set of channels the user is in. Idle-time and
login-time tracking is deferred to phase 11 (the nick-list widget).
"""

from __future__ import annotations

from typing import Any, ClassVar

from quasseltui.sync.base import SyncObject, init_field, sync_slot


class IrcUser(SyncObject):
    CLASS_NAME: ClassVar[bytes] = b"IrcUser"

    def __init__(self, object_name: str) -> None:
        super().__init__(object_name)
        self.network_id, self.nick = _split_user_object_name(object_name)
        self.user: str = ""
        self.host: str = ""
        self.real_name: str = ""
        self.account: str = ""
        self.away: bool = False
        self.away_message: str = ""
        # Channel names (no network prefix) this user is currently in.
        # Maintained by the join/part slots below; seeded by `UserModes`-
        # style init fields from Network.IrcUsersAndChannels.
        self.channels: set[str] = set()

    # -- slot handlers ------------------------------------------------------

    # AIDEV-NOTE: Quassel's IrcUser objectName is built from `<netId>/<nick>`
    # at construction, but Quassel does NOT re-address the SyncObject when
    # the nick changes — the C++ IrcUser keeps its original objectName for
    # the lifetime of the user (see src/common/ircuser.cpp). So the
    # dispatcher registry key staying bound to the old nick is correct
    # behavior, not a bug. Leaving this note because it's a natural
    # source of confusion when reading the dispatcher's lookup-by-key
    # logic. If a future Quassel version DOES re-address on nick change,
    # we'd need to re-key `_objects` in `Dispatcher` from `setNick`.
    @sync_slot(b"setNick")
    def _sync_set_nick(self, nick: str) -> None:
        if nick:
            self.nick = str(nick)

    @sync_slot(b"setUser")
    def _sync_set_user(self, user: str) -> None:
        self.user = str(user) if user is not None else ""

    @sync_slot(b"setHost")
    def _sync_set_host(self, host: str) -> None:
        self.host = str(host) if host is not None else ""

    @sync_slot(b"setRealName")
    def _sync_set_real_name(self, real_name: str) -> None:
        self.real_name = str(real_name) if real_name is not None else ""

    @sync_slot(b"setAccount")
    def _sync_set_account(self, account: str) -> None:
        self.account = str(account) if account is not None else ""

    @sync_slot(b"setAway")
    def _sync_set_away(self, away: bool) -> None:
        self.away = bool(away)

    @sync_slot(b"setAwayMessage")
    def _sync_set_away_message(self, message: str) -> None:
        self.away_message = str(message) if message is not None else ""

    @sync_slot(b"joinChannel")
    def _sync_join_channel(self, channel: str) -> None:
        if channel:
            self.channels.add(str(channel))

    @sync_slot(b"partChannel")
    def _sync_part_channel(self, channel: str) -> None:
        self.channels.discard(str(channel))

    @sync_slot(b"quit")
    def _sync_quit(self) -> None:
        # The user has left the network entirely. The dispatcher will also
        # remove us from its `(class_name, object_name)` registry; we clear
        # our own membership set so any straggler reference sees a clean
        # "nobody home" state.
        self.channels.clear()

    # -- init-field handlers ------------------------------------------------

    @init_field("nick")
    def _init_nick(self, value: Any) -> None:
        if value:
            self.nick = str(value)

    @init_field("user")
    def _init_user(self, value: Any) -> None:
        self.user = str(value) if value is not None else ""

    @init_field("host")
    def _init_host(self, value: Any) -> None:
        self.host = str(value) if value is not None else ""

    @init_field("realName")
    def _init_real_name(self, value: Any) -> None:
        self.real_name = str(value) if value is not None else ""

    @init_field("account")
    def _init_account(self, value: Any) -> None:
        self.account = str(value) if value is not None else ""

    @init_field("away")
    def _init_away(self, value: Any) -> None:
        self.away = bool(value)

    @init_field("awayMessage")
    def _init_away_message(self, value: Any) -> None:
        self.away_message = str(value) if value is not None else ""


def _split_user_object_name(object_name: str) -> tuple[int, str]:
    """Parse `"<netId>/<nick>"` into `(net_id, nick)`.

    Mirrors `_split_channel_object_name` from `irc_channel.py` — same
    forward-compat fallback (`net_id = -1` on a shape mismatch).
    """
    if "/" not in object_name:
        return -1, object_name
    prefix, _, nick = object_name.partition("/")
    try:
        return int(prefix), nick
    except ValueError:
        return -1, nick


__all__ = [
    "IrcUser",
]
