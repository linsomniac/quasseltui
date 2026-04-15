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

import datetime as _dt
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from quasseltui.protocol.enums import (
    FEATURE_LONG_TIME,
    FEATURE_RICH_MESSAGES,
    FEATURE_SENDER_PREFIXES,
    MessageFlag,
    MessageType,
)
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
# Message — `src/common/message.{h,cpp}`. The IRC message struct is the
# beating heart of every Quassel session and the thing we'll be decoding most.
#
# Wire order from `Message::operator<<`. Several fields are conditional on
# the negotiated feature set (the reader/writer pull this from the
# `QDataStreamReader.peer_features` attribute that the connection layer
# populates after the handshake):
#
#     msgId          : qint64
#     timestamp      : LongTime  → qint64 ms-since-epoch
#                      otherwise → quint32 sec-since-epoch
#     type           : quint32
#     flags          : quint8
#     bufferInfo     : BufferInfo (recursive call into _read_buffer_info)
#     sender         : QByteArray (UTF-8)
#     senderPrefixes : QByteArray (UTF-8)  — only if SenderPrefixes
#     realName       : QByteArray (UTF-8)  — only if RichMessages
#     avatarUrl      : QByteArray (UTF-8)  — only if RichMessages
#     contents       : QByteArray (UTF-8)
#
# Decoding is intentionally lenient on the trailing string fields: a malformed
# UTF-8 sender / contents shouldn't blow up the entire CONNECTED-state read
# loop, so we use `errors="replace"`. Truncated buffers (the frame ended
# mid-Message) still surface as `QDataStreamError` from the underlying
# datastream methods, which is what we want.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    """One IRC message as the core stores and replays it.

    `peer_features` is captured at decode time and stored alongside the
    fields that *might* be missing — this lets a downstream consumer tell
    "the core didn't send senderPrefixes (old core)" apart from "the core
    sent an empty senderPrefixes string (no IRC mode prefix)". Both decode
    to `senderPrefixes == ''`; we record the feature set so the difference
    is recoverable if anyone ever needs it.
    """

    msg_id: MsgId
    timestamp: _dt.datetime
    type: MessageType
    flags: MessageFlag
    buffer_info: BufferInfo
    sender: str
    sender_prefixes: str
    real_name: str
    avatar_url: str
    contents: str
    peer_features: frozenset[str] = field(default_factory=frozenset, repr=False)


def _read_message(reader: QDataStreamReader) -> Message:
    features = reader.peer_features
    msg_id = reader.read_int64()
    if FEATURE_LONG_TIME in features:
        ts_ms = reader.read_int64()
        timestamp = _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.UTC)
    else:
        ts_sec = reader.read_uint32()
        timestamp = _dt.datetime.fromtimestamp(ts_sec, tz=_dt.UTC)
    type_value = reader.read_uint32()
    try:
        type_enum = MessageType(type_value)
    except ValueError:
        # Forward-compatible: if the core grows a new message type we'd rather
        # surface it as Plain than crash the receive loop.
        type_enum = MessageType.Plain
    flags_value = reader.read_uint8()
    flags_enum = MessageFlag(flags_value & 0xFF)
    buffer_info = _read_buffer_info(reader)
    sender_bytes = reader.read_qbytearray() or b""
    if FEATURE_SENDER_PREFIXES in features:
        sender_prefixes_bytes = reader.read_qbytearray() or b""
    else:
        sender_prefixes_bytes = b""
    if FEATURE_RICH_MESSAGES in features:
        real_name_bytes = reader.read_qbytearray() or b""
        avatar_url_bytes = reader.read_qbytearray() or b""
    else:
        real_name_bytes = b""
        avatar_url_bytes = b""
    contents_bytes = reader.read_qbytearray() or b""
    return Message(
        msg_id=MsgId(msg_id),
        timestamp=timestamp,
        type=type_enum,
        flags=flags_enum,
        buffer_info=buffer_info,
        sender=sender_bytes.decode("utf-8", errors="replace"),
        sender_prefixes=sender_prefixes_bytes.decode("utf-8", errors="replace"),
        real_name=real_name_bytes.decode("utf-8", errors="replace"),
        avatar_url=avatar_url_bytes.decode("utf-8", errors="replace"),
        contents=contents_bytes.decode("utf-8", errors="replace"),
        peer_features=frozenset(features),
    )


def _write_message(writer: QDataStreamWriter, value: Any) -> None:
    if not isinstance(value, Message):
        raise QDataStreamError(f"Message writer received {type(value).__name__}, expected Message")
    features = writer.peer_features
    writer.write_int64(int(value.msg_id))
    if FEATURE_LONG_TIME in features:
        # Round to nearest millisecond. Python's datetime.timestamp() returns
        # float seconds; multiplying by 1000 and casting to int truncates,
        # which is fine for our fidelity needs (we don't care about sub-ms).
        if value.timestamp.tzinfo is None:
            ts_ms = int(value.timestamp.replace(tzinfo=_dt.UTC).timestamp() * 1000)
        else:
            ts_ms = int(value.timestamp.timestamp() * 1000)
        writer.write_int64(ts_ms)
    else:
        if value.timestamp.tzinfo is None:
            ts_sec = int(value.timestamp.replace(tzinfo=_dt.UTC).timestamp())
        else:
            ts_sec = int(value.timestamp.timestamp())
        writer.write_uint32(ts_sec)
    writer.write_uint32(int(value.type))
    writer.write_uint8(int(value.flags) & 0xFF)
    _write_buffer_info(writer, value.buffer_info)
    writer.write_qbytearray(value.sender.encode("utf-8"))
    if FEATURE_SENDER_PREFIXES in features:
        writer.write_qbytearray(value.sender_prefixes.encode("utf-8"))
    if FEATURE_RICH_MESSAGES in features:
        writer.write_qbytearray(value.real_name.encode("utf-8"))
        writer.write_qbytearray(value.avatar_url.encode("utf-8"))
    writer.write_qbytearray(value.contents.encode("utf-8"))


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
USER_TYPE_MESSAGE = b"Message"


def _register_all() -> None:
    # `py_type=` populates the Python-type → user-type-name map in
    # `quasseltui.qt.usertypes` so `write_variant(writer, buf_info)`
    # can auto-route through the UserType envelope. Identity is
    # deliberately excluded — its Python representation is a plain
    # `dict`, which would shadow `QVariantMap` handling and break
    # every SignalProxy map parameter we pass through.
    register_user_type(USER_TYPE_BUFFER_ID, _read_buffer_id, _write_buffer_id, py_type=BufferId)
    register_user_type(USER_TYPE_NETWORK_ID, _read_network_id, _write_network_id, py_type=NetworkId)
    register_user_type(
        USER_TYPE_IDENTITY_ID, _read_identity_id, _write_identity_id, py_type=IdentityId
    )
    register_user_type(USER_TYPE_USER_ID, _read_user_id, _write_user_id, py_type=UserId)
    register_user_type(USER_TYPE_ACCOUNT_ID, _read_account_id, _write_account_id, py_type=AccountId)
    register_user_type(USER_TYPE_MSG_ID, _read_msg_id, _write_msg_id, py_type=MsgId)
    register_user_type(
        USER_TYPE_BUFFER_INFO, _read_buffer_info, _write_buffer_info, py_type=BufferInfo
    )
    register_user_type(USER_TYPE_IDENTITY, _read_identity, _write_identity)
    register_user_type(USER_TYPE_MESSAGE, _read_message, _write_message, py_type=Message)


_register_all()


__all__ = [
    "USER_TYPE_ACCOUNT_ID",
    "USER_TYPE_BUFFER_ID",
    "USER_TYPE_BUFFER_INFO",
    "USER_TYPE_IDENTITY",
    "USER_TYPE_IDENTITY_ID",
    "USER_TYPE_MESSAGE",
    "USER_TYPE_MSG_ID",
    "USER_TYPE_NETWORK_ID",
    "USER_TYPE_USER_ID",
    "AccountId",
    "BufferId",
    "BufferInfo",
    "BufferType",
    "IdentityId",
    "Message",
    "MsgId",
    "NetworkId",
    "UserId",
]
