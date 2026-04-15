"""Registry for Qt `QVariant<UserType>` payloads.

Qt has two kinds of meta-types: the built-ins (Bool, Int, QString, ...) which
have stable numeric IDs, and user-defined types which all share the single ID
`QMetaType::User` (127). To tell user types apart on the wire, the QVariant
envelope writes a type-name string before the payload:

    quint32 type_id = 127  (UserType)
    quint8  is_null
    QByteArray name        (e.g. b"BufferInfo", b"NetworkId", b"Identity")
    payload bytes          (whatever that type's QDataStream operator<< emits)

Quassel registers a handful of these (`BufferInfo`, `BufferId`, `NetworkId`,
`MsgId`, `IdentityId`, `Identity`, `Network::Server`, `Message`, ...) and
relies on `qRegisterMetaType<T>` matching across both ends. We mirror that
registry here so the QVariant envelope decoder can look up the right payload
codec by name.

This module is the generic *mechanism*. The Quassel-specific type definitions
live in `quasseltui.protocol.usertypes`, which imports this module and calls
`register_user_type` for each concrete type at import time. Keeping the layers
separate means `qt/` stays free of any IRC concepts.

The receive side strips trailing null bytes from the name before lookup —
older Qt/Quassel versions wrote the name with a terminating NUL byte (because
`QByteArray(const char*)` plus `qRegisterMetaType` stringification used the
underlying C string with the terminator), and we want to match either.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from quasseltui.qt.datastream import QDataStreamError, QDataStreamReader, QDataStreamWriter

UserTypeReader = Callable[[QDataStreamReader], Any]
UserTypeWriter = Callable[[QDataStreamWriter, Any], None]


_USER_TYPE_READERS: dict[bytes, UserTypeReader] = {}
_USER_TYPE_WRITERS: dict[bytes, UserTypeWriter] = {}
# Reverse lookup: a registered Python class → its on-wire user-type name.
# Used by `write_variant` so a caller passing e.g. a `BufferInfo` dataclass
# doesn't have to explicitly specify `user_type_name=b"BufferInfo"`; the
# envelope is inferred from the value's Python type. Populated only for
# types with an unambiguous mapping (`Identity` is deliberately *not*
# here because its payload is a plain `dict`, which collides with
# `QVariantMap` handling).
_PY_TYPE_TO_NAME: dict[type, bytes] = {}


def register_user_type(
    name: bytes,
    reader: UserTypeReader,
    writer: UserTypeWriter,
    *,
    py_type: type | None = None,
) -> None:
    """Register codecs for a user-type by its on-wire name.

    `name` is the bytes you'd see in the QVariant envelope after stripping
    any trailing null byte — typically just the ASCII type name like
    `b"BufferInfo"`. Registering the same name twice is allowed and last
    wins; this makes the registry test-friendly.

    `py_type` is an optional Python class whose instances should be
    serialized through this user-type envelope when passed directly to
    `write_variant` without an explicit `user_type_name`. Supply it only
    when the mapping is unambiguous — do *not* set it for types whose
    instances collide with a built-in primitive or container (for
    example `Identity`, whose Python representation is `dict` and
    would shadow `QVariantMap` handling).
    """
    _USER_TYPE_READERS[name] = reader
    _USER_TYPE_WRITERS[name] = writer
    if py_type is not None:
        _PY_TYPE_TO_NAME[py_type] = name


def is_registered(name: bytes) -> bool:
    return name in _USER_TYPE_READERS


def name_for_python_value(value: Any) -> bytes | None:
    """Return the registered user-type name for `value`, or `None`.

    Used by `quasseltui.qt.variant.write_variant` to route dataclass
    values (e.g. `BufferInfo`) through the UserType envelope without
    the caller having to spell out the name on every call site. Uses
    `type(value)` rather than `isinstance` so a subclass doesn't
    silently inherit the mapping — Quassel user-types are leaf types
    in practice, and letting a subclass dispatch to the base-class
    codec would be a surprising footgun if anyone ever adds one.
    """
    return _PY_TYPE_TO_NAME.get(type(value))


def read_user_type_payload(reader: QDataStreamReader, name: bytes) -> Any:
    """Decode the payload that follows the user-type name.

    The caller must have already consumed the envelope (type id, is_null
    flag, the QByteArray name itself). Raises `QDataStreamError` if the type
    isn't registered — silently returning `None` would let the surrounding
    stream desynchronize, since we wouldn't know how many bytes to skip.
    """
    fn = _USER_TYPE_READERS.get(name)
    if fn is None:
        raise QDataStreamError(
            f"unsupported QVariant<UserType> name {name!r} at offset {reader.position}; "
            "no codec registered (see quasseltui.protocol.usertypes)"
        )
    return fn(reader)


def write_user_type_payload(writer: QDataStreamWriter, name: bytes, value: Any) -> None:
    """Encode the payload portion of a user-type envelope.

    Caller is responsible for the envelope (type id 127, is_null=0, name as
    QByteArray) — `quasseltui.qt.variant.write_variant` does that, then
    delegates to this function.
    """
    fn = _USER_TYPE_WRITERS.get(name)
    if fn is None:
        raise QDataStreamError(f"cannot write QVariant<UserType>: name {name!r} is not registered")
    fn(writer, value)


def _strip_trailing_nulls(name: bytes) -> bytes:
    """Mirror Quassel's `serializers.cpp::deserializeQVariant` cleanup.

    Older clients/cores wrote the type name including the NUL terminator from
    the underlying C string. The deserializer drops trailing NULs so a name
    like `b"NetworkId\\x00"` resolves the same as `b"NetworkId"`.
    """
    return name.rstrip(b"\x00")


def normalize_name(name: bytes) -> bytes:
    """Public alias for the trailing-null cleanup, used by the variant decoder."""
    return _strip_trailing_nulls(name)
