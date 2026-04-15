"""Quassel custom `QVariant<UserType>` payloads.

Quassel registers a handful of typedefs and structs as Qt user types so they
round-trip through `QVariant`. We mirror each one here as a frozen dataclass
plus reader/writer functions, then register them with the generic
`quasseltui.qt.usertypes` registry on import.

The on-wire shape for each type is taken from the Quassel C++ source — every
type listed here has its own `QDataStream& operator<<(QDataStream&, const T&)`
in `src/common/`. The payload is always *just* the body bytes; the QVariant
envelope (type id 127, is_null byte, name as QByteArray) is added by
`quasseltui.qt.variant.write_variant` when called with `user_type_name=...`.

Importing this module registers everything as a side effect. Code that needs
to decode QVariants containing Quassel user types should `import
quasseltui.protocol.usertypes  # noqa: F401` somewhere in its import chain.
The `quasseltui.protocol.handshake` and `quasseltui.protocol.messages`
modules already do this for the handshake-state messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from quasseltui.qt.datastream import QDataStreamError, QDataStreamReader, QDataStreamWriter
from quasseltui.qt.usertypes import register_user_type
from quasseltui.qt.variant import read_qvariantmap, write_qvariantmap

# ---------------------------------------------------------------------------
# Identifier typedefs — `struct BufferId : public SignedId` and friends in
# `src/common/types.h`. SignedId carries a single qint32; SignedId64 a qint64.
# We use frozen dataclasses (rather than NewType ints) so the dispatch-by-type
# in `_dispatch_identifier_value` is unambiguous and the repr is informative
# during debugging.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class BufferId:
    """Quassel `BufferId` — a 32-bit signed identifier for a buffer row."""

    value: int

    def __int__(self) -> int:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class NetworkId:
    """Quassel `NetworkId` — 32-bit signed identifier for a Network row."""

    value: int

    def __int__(self) -> int:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class IdentityId:
    """Quassel `IdentityId` — 32-bit signed identifier for an Identity row."""

    value: int

    def __int__(self) -> int:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class UserId:
    """Quassel `UserId` — 32-bit signed identifier for a core user account."""

    value: int

    def __int__(self) -> int:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class AccountId:
    """Quassel `AccountId` — 32-bit signed identifier (client-side accounts)."""

    value: int

    def __int__(self) -> int:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class MsgId:
    """Quassel `MsgId` — 64-bit signed identifier for a Message row.

    Older cores used qint32 here; modern cores promoted this to qint64 to
    avoid running out of message IDs. We always read/write 64-bit since
    Quassel pinned the wire format at the Qt 5 typedef.
    """

    value: int

    def __int__(self) -> int:
        return self.value


# ---------------------------------------------------------------------------
# BufferInfo — `src/common/bufferinfo.{h,cpp}`.
#
# Wire order (`operator<<`):
#     bufferId : qint32
#     netid    : qint32
#     type     : qint16  (BufferType enum, see below)
#     groupId  : quint32
#     name     : QByteArray (UTF-8 of the QString bufferName)
# ---------------------------------------------------------------------------


class BufferType(IntEnum):
    """Mirror of `BufferInfo::Type` in `src/common/bufferinfo.h`.

    The values look bitfield-shaped (1, 2, 4, 8) but the C++ enum stores a
    single value per buffer rather than ORing them. We keep the IntEnum
    semantics — Quassel never asserts compound types on the wire.
    """

    Invalid = 0x00
    Status = 0x01
    Channel = 0x02
    Query = 0x04
    Group = 0x08


@dataclass(frozen=True, slots=True)
class BufferInfo:
    """One Quassel buffer row.

    `name` is decoded from UTF-8 into a Python `str`. Quassel emits the
    bufferName field as `QString::toUtf8()` rather than `QString` (which
    would be UTF-16BE); we mirror that on encode.
    """

    buffer_id: BufferId
    network_id: NetworkId
    type: BufferType
    group_id: int
    name: str


# ---------------------------------------------------------------------------
# Codec functions for the user-type registry. Each pair is just a thin
# wrapper around the QDataStream primitives.
# ---------------------------------------------------------------------------


def _read_buffer_id(reader: QDataStreamReader) -> BufferId:
    return BufferId(reader.read_int32())


def _write_buffer_id(writer: QDataStreamWriter, value: Any) -> None:
    writer.write_int32(int(value))


def _read_network_id(reader: QDataStreamReader) -> NetworkId:
    return NetworkId(reader.read_int32())


def _write_network_id(writer: QDataStreamWriter, value: Any) -> None:
    writer.write_int32(int(value))


def _read_identity_id(reader: QDataStreamReader) -> IdentityId:
    return IdentityId(reader.read_int32())


def _write_identity_id(writer: QDataStreamWriter, value: Any) -> None:
    writer.write_int32(int(value))


def _read_user_id(reader: QDataStreamReader) -> UserId:
    return UserId(reader.read_int32())


def _write_user_id(writer: QDataStreamWriter, value: Any) -> None:
    writer.write_int32(int(value))


def _read_account_id(reader: QDataStreamReader) -> AccountId:
    return AccountId(reader.read_int32())


def _write_account_id(writer: QDataStreamWriter, value: Any) -> None:
    writer.write_int32(int(value))


def _read_msg_id(reader: QDataStreamReader) -> MsgId:
    return MsgId(reader.read_int64())


def _write_msg_id(writer: QDataStreamWriter, value: Any) -> None:
    writer.write_int64(int(value))


def _read_buffer_info(reader: QDataStreamReader) -> BufferInfo:
    buffer_id = reader.read_int32()
    network_id = reader.read_int32()
    type_value = reader.read_int16()
    group_id = reader.read_uint32()
    name_bytes = reader.read_qbytearray()
    name = "" if name_bytes is None else name_bytes.decode("utf-8", errors="replace")
    try:
        type_enum = BufferType(type_value)
    except ValueError:
        # Forward-compatible: a future Quassel might add a buffer kind we
        # don't recognize. Decoding the rest of the stream is more important
        # than rejecting the buffer outright, so we coerce to Invalid and
        # keep the raw value in `group_id` for debugging if needed.
        type_enum = BufferType.Invalid
    return BufferInfo(
        buffer_id=BufferId(buffer_id),
        network_id=NetworkId(network_id),
        type=type_enum,
        group_id=group_id,
        name=name,
    )


def _write_buffer_info(writer: QDataStreamWriter, value: Any) -> None:
    if not isinstance(value, BufferInfo):
        raise QDataStreamError(
            f"BufferInfo writer received {type(value).__name__}, expected BufferInfo"
        )
    writer.write_int32(int(value.buffer_id))
    writer.write_int32(int(value.network_id))
    writer.write_int16(int(value.type))
    writer.write_uint32(int(value.group_id))
    writer.write_qbytearray(value.name.encode("utf-8"))


# ---------------------------------------------------------------------------
# Identity — Quassel registers `Identity` as a user type whose payload is a
# serialized `QVariantMap` (Identity::toVariantMap()). We don't decode the
# individual fields yet; phase 5 will model Identity properly. For now we
# expose the raw map so the CLI can count identities and print their names.
# ---------------------------------------------------------------------------


def _read_identity(reader: QDataStreamReader) -> dict[str, Any]:
    return read_qvariantmap(reader)


def _write_identity(writer: QDataStreamWriter, value: Any) -> None:
    if not isinstance(value, dict):
        raise QDataStreamError(f"Identity writer received {type(value).__name__}, expected dict")
    write_qvariantmap(writer, value)


# ---------------------------------------------------------------------------
# Registry hookup. The names match `qRegisterMetaType<T>("...")` in the
# Quassel source — these strings are what travels on the wire as the
# QByteArray inside the QVariant<UserType> envelope.
# ---------------------------------------------------------------------------


# The registered name strings come straight from `qRegisterMetaType<T>("...")`
# calls scattered across the Quassel source tree. They're all plain ASCII so
# the bytes are stable and don't need encoding.
USER_TYPE_BUFFER_ID = b"BufferId"
USER_TYPE_NETWORK_ID = b"NetworkId"
USER_TYPE_IDENTITY_ID = b"IdentityId"
USER_TYPE_USER_ID = b"UserId"
USER_TYPE_ACCOUNT_ID = b"AccountId"
USER_TYPE_MSG_ID = b"MsgId"
USER_TYPE_BUFFER_INFO = b"BufferInfo"
USER_TYPE_IDENTITY = b"Identity"


def _register_all() -> None:
    register_user_type(USER_TYPE_BUFFER_ID, _read_buffer_id, _write_buffer_id)
    register_user_type(USER_TYPE_NETWORK_ID, _read_network_id, _write_network_id)
    register_user_type(USER_TYPE_IDENTITY_ID, _read_identity_id, _write_identity_id)
    register_user_type(USER_TYPE_USER_ID, _read_user_id, _write_user_id)
    register_user_type(USER_TYPE_ACCOUNT_ID, _read_account_id, _write_account_id)
    register_user_type(USER_TYPE_MSG_ID, _read_msg_id, _write_msg_id)
    register_user_type(USER_TYPE_BUFFER_INFO, _read_buffer_info, _write_buffer_info)
    register_user_type(USER_TYPE_IDENTITY, _read_identity, _write_identity)


_register_all()


__all__ = [
    "USER_TYPE_ACCOUNT_ID",
    "USER_TYPE_BUFFER_ID",
    "USER_TYPE_BUFFER_INFO",
    "USER_TYPE_IDENTITY",
    "USER_TYPE_IDENTITY_ID",
    "USER_TYPE_MSG_ID",
    "USER_TYPE_NETWORK_ID",
    "USER_TYPE_USER_ID",
    "AccountId",
    "BufferId",
    "BufferInfo",
    "BufferType",
    "IdentityId",
    "MsgId",
    "NetworkId",
    "UserId",
]
