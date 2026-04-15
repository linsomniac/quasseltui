"""QVariant read/write dispatch.

A QVariant on the wire is:

    quint32 type_id
    quint8  is_null
    payload (only present when not null and type_id != Invalid)

The dispatch table below maps `QMetaType` IDs to a `(reader, writer)` pair.
Container types (QVariantList, QVariantMap, QStringList) are recursive over
QVariant so they live here rather than in a separate module.

This file grows on demand. Phase 1 covers only the types needed for `ClientInit`
plus their immediate companions: Bool, Int, UInt, LongLong, ULongLong, QString,
QStringList, QByteArray, QVariantList, QVariantMap.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from quasseltui.qt.datastream import QDataStreamError, QDataStreamReader, QDataStreamWriter
from quasseltui.qt.types import QMetaType

QVariantMap = dict[str, Any]
QVariantList = list[Any]


# ---------------------------------------------------------------------------
# Container codecs (recursive on QVariant)
# ---------------------------------------------------------------------------


def read_qvariantlist(reader: QDataStreamReader) -> QVariantList:
    count = reader.read_uint32()
    return [read_variant(reader) for _ in range(count)]


def write_qvariantlist(writer: QDataStreamWriter, value: Sequence[Any]) -> None:
    writer.write_uint32(len(value))
    for item in value:
        write_variant(writer, item)


def read_qvariantmap(reader: QDataStreamReader) -> QVariantMap:
    count = reader.read_uint32()
    out: QVariantMap = {}
    for _ in range(count):
        key = reader.read_qstring()
        if key is None:
            raise QDataStreamError("QVariantMap key is a null QString")
        out[key] = read_variant(reader)
    return out


def write_qvariantmap(writer: QDataStreamWriter, value: Mapping[str, Any]) -> None:
    writer.write_uint32(len(value))
    for key, val in value.items():
        writer.write_qstring(key)
        write_variant(writer, val)


def read_qstringlist(reader: QDataStreamReader) -> list[str]:
    count = reader.read_uint32()
    out: list[str] = []
    for _ in range(count):
        s = reader.read_qstring()
        if s is None:
            raise QDataStreamError("QStringList element is a null QString")
        out.append(s)
    return out


def write_qstringlist(writer: QDataStreamWriter, value: Sequence[str]) -> None:
    writer.write_uint32(len(value))
    for s in value:
        writer.write_qstring(s)


# ---------------------------------------------------------------------------
# Primitive type codecs (wrappers around the datastream methods so they share
# the same `(reader,) -> value` / `(writer, value) -> None` signature as the
# container codecs and can live in the dispatch table.)
# ---------------------------------------------------------------------------


def _read_bool(reader: QDataStreamReader) -> bool:
    return reader.read_bool()


def _write_bool(writer: QDataStreamWriter, value: Any) -> None:
    writer.write_bool(bool(value))


def _read_int32(reader: QDataStreamReader) -> int:
    return reader.read_int32()


def _write_int32(writer: QDataStreamWriter, value: Any) -> None:
    writer.write_int32(int(value))


def _read_uint32(reader: QDataStreamReader) -> int:
    return reader.read_uint32()


def _write_uint32(writer: QDataStreamWriter, value: Any) -> None:
    writer.write_uint32(int(value))


def _read_int64(reader: QDataStreamReader) -> int:
    return reader.read_int64()


def _write_int64(writer: QDataStreamWriter, value: Any) -> None:
    writer.write_int64(int(value))


def _read_uint64(reader: QDataStreamReader) -> int:
    return reader.read_uint64()


def _write_uint64(writer: QDataStreamWriter, value: Any) -> None:
    writer.write_uint64(int(value))


def _read_qstring(reader: QDataStreamReader) -> str | None:
    return reader.read_qstring()


def _write_qstring(writer: QDataStreamWriter, value: Any) -> None:
    writer.write_qstring(None if value is None else str(value))


def _read_qbytearray(reader: QDataStreamReader) -> bytes | None:
    return reader.read_qbytearray()


def _write_qbytearray(writer: QDataStreamWriter, value: Any) -> None:
    if value is None:
        writer.write_qbytearray(None)
    elif isinstance(value, bytes | bytearray | memoryview):
        writer.write_qbytearray(bytes(value))
    else:
        raise TypeError(f"cannot serialize {type(value).__name__} as QByteArray")


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


ReaderFn = Callable[[QDataStreamReader], Any]
WriterFn = Callable[[QDataStreamWriter, Any], None]


_READERS: dict[int, ReaderFn] = {
    QMetaType.Bool: _read_bool,
    QMetaType.Int: _read_int32,
    QMetaType.UInt: _read_uint32,
    QMetaType.LongLong: _read_int64,
    QMetaType.ULongLong: _read_uint64,
    QMetaType.QString: _read_qstring,
    QMetaType.QByteArray: _read_qbytearray,
    QMetaType.QVariantMap: read_qvariantmap,
    QMetaType.QVariantList: read_qvariantlist,
    QMetaType.QStringList: read_qstringlist,
}


_WRITERS: dict[int, WriterFn] = {
    QMetaType.Bool: _write_bool,
    QMetaType.Int: _write_int32,
    QMetaType.UInt: _write_uint32,
    QMetaType.LongLong: _write_int64,
    QMetaType.ULongLong: _write_uint64,
    QMetaType.QString: _write_qstring,
    QMetaType.QByteArray: _write_qbytearray,
    QMetaType.QVariantMap: write_qvariantmap,
    QMetaType.QVariantList: write_qvariantlist,
    QMetaType.QStringList: write_qstringlist,
}


def register_type(
    type_id: int,
    reader: ReaderFn,
    writer: WriterFn,
) -> None:
    """Register a reader/writer pair for an additional QMetaType ID.

    Later phases use this for Quassel custom user types (BufferInfo, Message,
    ...). Phase 1 only ships the standard types above.
    """
    _READERS[type_id] = reader
    _WRITERS[type_id] = writer


# ---------------------------------------------------------------------------
# QVariant envelope
# ---------------------------------------------------------------------------


def read_variant(reader: QDataStreamReader) -> Any:
    """Decode a single QVariant envelope from the stream."""
    type_id = reader.read_uint32()
    is_null = reader.read_uint8()
    if type_id == QMetaType.Invalid:
        return None
    if is_null:
        return None
    fn = _READERS.get(type_id)
    if fn is None:
        raise QDataStreamError(
            f"unsupported QVariant type id {type_id} at offset {reader.position}"
        )
    return fn(reader)


def write_variant(
    writer: QDataStreamWriter,
    value: Any,
    type_id: int | None = None,
) -> None:
    """Encode a single QVariant envelope.

    If `type_id` is omitted, infer it from the Python type of `value`. Use the
    explicit form when you need to force a particular wire type (e.g., serialize
    a Python `int` as a `QMetaType.UInt` or `LongLong` rather than the default
    `Int`).
    """
    if value is None and type_id is None:
        writer.write_uint32(QMetaType.Invalid)
        writer.write_uint8(1)
        return

    if type_id is None:
        type_id = _infer_type_id(value)

    writer.write_uint32(type_id)
    if value is None:
        writer.write_uint8(1)
        return
    writer.write_uint8(0)

    fn = _WRITERS.get(type_id)
    if fn is None:
        raise QDataStreamError(f"unsupported QVariant type id {type_id} for write")
    fn(writer, value)


def _infer_type_id(value: Any) -> int:
    # bool must come before int — `bool` is a subclass of `int` in Python.
    if isinstance(value, bool):
        return QMetaType.Bool
    if isinstance(value, int):
        return QMetaType.Int
    if isinstance(value, str):
        return QMetaType.QString
    if isinstance(value, bytes | bytearray | memoryview):
        return QMetaType.QByteArray
    if isinstance(value, Mapping):
        return QMetaType.QVariantMap
    if isinstance(value, list | tuple):
        return QMetaType.QVariantList
    raise TypeError(f"cannot infer QVariant type for {type(value).__name__}")
