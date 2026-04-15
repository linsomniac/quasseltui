"""Quassel `Identity` syncable — one identity (nick/realname/away config).

Quassel's `Identity` carries a lot of fields (away-nicks list, detach/away
messages, part/quit/kick messages, auto-away timers, SSL cert/key). The v1
TUI only cares about the display name, primary nicks, and real name, but we
still stash the raw field values for anything we don't model so debug tools
can poke at them.

Object-name convention: the stringified `IdentityId` — the core reports this
as a qint32 and the C++ `Identity::objectName()` builds the string from it.
"""

from __future__ import annotations

from typing import Any, ClassVar

from quasseltui.sync.base import SyncObject, init_field, sync_slot


class Identity(SyncObject):
    CLASS_NAME: ClassVar[bytes] = b"Identity"

    def __init__(self, object_name: str) -> None:
        super().__init__(object_name)
        self.identity_id: int = _maybe_int(object_name)
        self.identity_name: str = ""
        self.real_name: str = ""
        self.ident: str = ""
        self.nicks: list[str] = []
        self.away_nick: str = ""
        # Raw dict of everything else the core sent. Useful for dump-state
        # debugging and for forward-compatibility with newer cores that add
        # fields we haven't modelled yet.
        self.extra: dict[str, Any] = {}

    # -- slot handlers ------------------------------------------------------

    @sync_slot(b"setIdentityName")
    def _sync_set_identity_name(self, name: str) -> None:
        self.identity_name = str(name) if name is not None else ""

    @sync_slot(b"setRealName")
    def _sync_set_real_name(self, real_name: str) -> None:
        self.real_name = str(real_name) if real_name is not None else ""

    @sync_slot(b"setIdent")
    def _sync_set_ident(self, ident: str) -> None:
        self.ident = str(ident) if ident is not None else ""

    @sync_slot(b"setNicks")
    def _sync_set_nicks(self, nicks: list[str]) -> None:
        if isinstance(nicks, list):
            self.nicks = [str(n) for n in nicks]

    # -- init-field handlers ------------------------------------------------

    @init_field("identityName")
    def _init_identity_name(self, value: Any) -> None:
        self.identity_name = str(value) if value is not None else ""

    @init_field("realName")
    def _init_real_name(self, value: Any) -> None:
        self.real_name = str(value) if value is not None else ""

    @init_field("ident")
    def _init_ident(self, value: Any) -> None:
        self.ident = str(value) if value is not None else ""

    @init_field("nicks")
    def _init_nicks(self, value: Any) -> None:
        if isinstance(value, list):
            self.nicks = [str(n) for n in value]

    @init_field("awayNick")
    def _init_away_nick(self, value: Any) -> None:
        self.away_nick = str(value) if value is not None else ""

    def apply_init_field(self, key: str, value: Any) -> None:
        """Route known fields through decorators, stash the rest in `extra`.

        Overriding rather than relying on the base implementation lets
        Identity act as a catch-all that preserves every wire field — the
        plan explicitly calls out that Identity is "raw-map-backed" for
        phase 5.
        """
        if key in type(self)._init_fields:
            super().apply_init_field(key, value)
            return
        self.extra[key] = value


def _maybe_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


__all__ = [
    "Identity",
]
