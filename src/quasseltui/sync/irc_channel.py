"""Quassel `IrcChannel` syncable — one joined channel on a network.

Object-name convention: `"<networkId>/<channelName>"` (e.g. `"1/#python"`),
which is what Quassel's C++ `IrcChannel::objectName()` builds when it ships
a Sync or InitData frame. We stash the raw parts on the instance so the
dispatcher can walk back to the owning `Network` without parsing strings.

Scope for phase 5 is limited to rendering: channel name, topic, and the set
of members. Op/voice modes, +k passwords, and per-mode-arg channel modes
are stored but not yet interpreted — phase 11 will add them to the nick-list
widget.
"""

from __future__ import annotations

from typing import Any, ClassVar

from quasseltui.sync.base import SyncObject, init_field, sync_slot


class IrcChannel(SyncObject):
    CLASS_NAME: ClassVar[bytes] = b"IrcChannel"

    def __init__(self, object_name: str) -> None:
        super().__init__(object_name)
        self.network_id, self.name = _split_channel_object_name(object_name)
        self.topic: str = ""
        self.password: str = ""
        self.encrypted: bool = False
        # Per-user mode string (e.g. `"@+"`) keyed by nick. Empty string
        # means "in the channel, no prefix modes". Absence means "not in
        # the channel" from our point of view.
        self.user_modes: dict[str, str] = {}
        # Parallel parsed record of the channel modes string. We only keep
        # the raw `+stni`-style list of mode chars + a parallel map for the
        # parameterised ones; the TUI doesn't need a full parser here.
        self.channel_modes: str = ""

    @property
    def members(self) -> set[str]:
        return set(self.user_modes.keys())

    # -- slot handlers ------------------------------------------------------

    @sync_slot(b"setTopic")
    def _sync_set_topic(self, topic: str) -> None:
        self.topic = str(topic) if topic is not None else ""

    @sync_slot(b"setPassword")
    def _sync_set_password(self, password: str) -> None:
        self.password = str(password) if password is not None else ""

    @sync_slot(b"setEncrypted")
    def _sync_set_encrypted(self, encrypted: bool) -> None:
        self.encrypted = bool(encrypted)

    @sync_slot(b"joinIrcUsers")
    def _sync_join_irc_users(self, users: Any, modes: Any) -> None:
        """Batch join — `users` and `modes` are parallel lists.

        Typed as `Any` because slot params are whatever the QVariant codec
        happened to decode; we narrow to list here so a buggy core sending
        e.g. a lone string doesn't crash the dispatcher. `modes[i]` is the
        initial mode prefix for `users[i]` (empty for a plain join, `"@"`
        for a pre-existing op). Shorter `modes` is padded with `""`.
        """
        if not isinstance(users, list):
            return
        if not isinstance(modes, list):
            modes = []
        for i, user in enumerate(users):
            mode = modes[i] if i < len(modes) else ""
            nick = str(user).split("!", 1)[0]
            if nick:
                self.user_modes[nick] = str(mode) if mode is not None else ""

    @sync_slot(b"part")
    def _sync_part(self, user: str) -> None:
        nick = str(user).split("!", 1)[0]
        self.user_modes.pop(nick, None)

    @sync_slot(b"addUserMode")
    def _sync_add_user_mode(self, user: str, mode: str) -> None:
        nick = str(user).split("!", 1)[0]
        if not nick:
            return
        existing = self.user_modes.get(nick, "")
        if mode and mode not in existing:
            self.user_modes[nick] = existing + str(mode)

    @sync_slot(b"removeUserMode")
    def _sync_remove_user_mode(self, user: str, mode: str) -> None:
        nick = str(user).split("!", 1)[0]
        if not nick:
            return
        existing = self.user_modes.get(nick, "")
        self.user_modes[nick] = existing.replace(str(mode or ""), "")

    # -- init-field handlers ------------------------------------------------

    @init_field("name")
    def _init_name(self, value: Any) -> None:
        if value:
            self.name = str(value)

    @init_field("topic")
    def _init_topic(self, value: Any) -> None:
        self.topic = str(value) if value is not None else ""

    @init_field("password")
    def _init_password(self, value: Any) -> None:
        self.password = str(value) if value is not None else ""

    @init_field("encrypted")
    def _init_encrypted(self, value: Any) -> None:
        self.encrypted = bool(value)

    @init_field("UserModes")
    def _init_user_modes(self, value: Any) -> None:
        """Initial `{nick: modeString}` membership map.

        Unlike the slot flavour which incrementally adds / removes, this
        replaces the current roster wholesale — InitData is always a full
        snapshot. Non-string values are coerced; anything non-dict is
        ignored so a malformed wire payload doesn't crash the channel.
        """
        if not isinstance(value, dict):
            return
        self.user_modes = {str(nick): str(mode or "") for nick, mode in value.items()}

    @init_field("ChanModes")
    def _init_chan_modes(self, value: Any) -> None:
        if isinstance(value, str):
            self.channel_modes = value


def _split_channel_object_name(object_name: str) -> tuple[int, str]:
    """Parse `"<netId>/<name>"` into `(net_id, channel_name)`.

    Returns `(-1, object_name)` if the string doesn't match the expected
    shape — e.g. an older core, a test that constructs the instance
    directly, or a forward-compat object that we don't understand. The
    caller can detect this via `network_id == -1`.
    """
    sep = object_name.find("/")
    if sep < 0:
        return -1, object_name
    prefix, _, name = object_name.partition("/")
    try:
        return int(prefix), name
    except ValueError:
        return -1, name


__all__ = [
    "IrcChannel",
]
