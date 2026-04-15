"""Serialize and deserialize handshake-state messages over the framed wire.

The DataStream protocol stores each handshake message as a `QVariantList`
where the items alternate between the field name (as a `QVariant<QByteArray>`
holding the UTF-8 bytes of the key) and the typed field value. Qt's
`QVariantMap` iteration is in sorted-key order, so to produce bytes that
match what a real Quassel client emits we sort our dict's keys before
flattening — and we read incoming messages by walking the list in pairs and
rebuilding the dict regardless of the on-wire order.

Source of truth for this format is
`DataStreamPeer::writeMessage(const QVariantMap&)` in the Quassel C++ tree:

    QVariantList list;
    QVariantMap::const_iterator it = handshakeMsg.begin();
    while (it != handshakeMsg.end()) {
        list << it.key().toUtf8() << it.value();
        ++it;
    }
    writeMessage(list);
"""

from __future__ import annotations

import asyncio
from typing import Any

from quasseltui.protocol.errors import HandshakeError
from quasseltui.protocol.framing import read_frame, write_frame
from quasseltui.protocol.messages import (
    CLIENT_INIT,
    ClientInit,
    ClientLogin,
    HandshakeMessage,
    parse_handshake_message,
)
from quasseltui.qt.datastream import QDataStreamError, QDataStreamReader, QDataStreamWriter
from quasseltui.qt.types import QMetaType
from quasseltui.qt.variant import read_qvariantlist, write_variant


def encode_handshake_payload(fields: dict[str, Any]) -> bytes:
    """Flatten a handshake field dict into a serialized QVariantList.

    Returned bytes are the frame *payload* — the caller is responsible for
    wrapping in the 4-byte length prefix. Keys are emitted as
    `QVariant<QByteArray>` (matching Qt's `key.toUtf8()`), values keep
    whatever inferred type `write_variant` picks unless the caller forced
    one with the explicit-typed sequences below.
    """
    writer = QDataStreamWriter()
    writer.write_uint32(len(fields) * 2)
    for key in sorted(fields):
        write_variant(writer, key.encode("utf-8"), type_id=QMetaType.QByteArray)
        value = fields[key]
        write_variant(writer, value, type_id=_explicit_type_for(key, value))
    return writer.to_bytes()


def decode_handshake_payload(payload: bytes) -> dict[str, Any]:
    """Inverse of `encode_handshake_payload`.

    The wire format is a flat QVariantList of `[key0, val0, key1, val1, ...]`
    so we walk it in pairs and rebuild a Python dict. Odd lengths are an
    immediate protocol error; non-string-decodable keys are too.
    """
    reader = QDataStreamReader(payload)
    items = read_qvariantlist(reader)
    if not reader.at_end():
        raise HandshakeError(f"trailing {reader.remaining()} bytes after handshake payload")
    if len(items) % 2 != 0:
        raise HandshakeError(
            f"handshake payload has odd item count {len(items)}; expected key/value pairs"
        )
    out: dict[str, Any] = {}
    for i in range(0, len(items), 2):
        raw_key = items[i]
        if isinstance(raw_key, bytes):
            try:
                key = raw_key.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HandshakeError(f"handshake key is not valid UTF-8: {exc}") from exc
        elif isinstance(raw_key, str):
            key = raw_key
        else:
            raise HandshakeError(f"handshake key has unexpected type {type(raw_key).__name__}")
        out[key] = items[i + 1]
    return out


def _explicit_type_for(key: str, value: Any) -> int | None:
    """Force specific QMetaType IDs for fields whose Python type is ambiguous.

    Two cases matter for ClientInit:

    - `Features` is a Python `int` but the core wants a `quint32` (UInt),
      not the default Int (which is `qint32` and would mis-encode the high
      bit of the legacy feature mask).
    - `FeatureList` is a Python list but the core wants `QStringList`
      (a flat list of unwrapped QStrings) rather than a `QVariantList` of
      `QVariant<QString>` items. Both decode to the same Python list on
      our side but only QStringList round-trips correctly through the
      core's typed accessor.

    Anything else falls back to `write_variant`'s type inference.
    """
    if key == "Features" and isinstance(value, int) and not isinstance(value, bool):
        return QMetaType.UInt
    if key == "FeatureList" and isinstance(value, list | tuple):
        return QMetaType.QStringList
    return None


def encode_client_init(msg: ClientInit) -> bytes:
    """Convenience: build a `ClientInit` payload ready to hand to `write_frame`."""
    return encode_handshake_payload(msg.to_map())


async def send_client_init(writer: asyncio.StreamWriter, msg: ClientInit) -> None:
    """Frame and send a `ClientInit` over an already-probed stream."""
    await write_frame(writer, encode_client_init(msg))


def encode_client_login(msg: ClientLogin) -> bytes:
    """Build a `ClientLogin` payload for the auth half of the handshake."""
    return encode_handshake_payload(msg.to_map())


async def send_client_login(writer: asyncio.StreamWriter, msg: ClientLogin) -> None:
    """Frame and send a `ClientLogin` over the post-ClientInitAck stream."""
    await write_frame(writer, encode_client_login(msg))


async def recv_handshake_message(
    reader: asyncio.StreamReader,
    *,
    max_frame_bytes: int | None = None,
) -> HandshakeMessage:
    """Read one framed handshake reply and decode it into a typed dataclass.

    `max_frame_bytes` is forwarded to `read_frame`; leave it `None` to use
    the default 64 MiB cap. Raises `HandshakeError` on malformed payloads
    and lets `QDataStreamError` propagate from the binary codec — both
    classes of failure should bubble up to the connection state machine,
    which handles them by closing the socket.

    `ClientLoginReject` is converted into an `AuthError` exception by
    `parse_handshake_message`, so callers do NOT need to special-case it
    — they only need to catch `AuthError` (a subclass of `HandshakeError`)
    if they want to distinguish credential failures from other handshake
    failures.
    """
    kwargs: dict[str, Any] = {}
    if max_frame_bytes is not None:
        kwargs["max_frame_bytes"] = max_frame_bytes
    payload = await read_frame(reader, **kwargs)
    try:
        fields = decode_handshake_payload(payload)
    except QDataStreamError as exc:
        raise HandshakeError(f"failed to decode handshake payload: {exc}") from exc
    return parse_handshake_message(fields)


__all__ = [
    "CLIENT_INIT",
    "decode_handshake_payload",
    "encode_client_init",
    "encode_client_login",
    "encode_handshake_payload",
    "recv_handshake_message",
    "send_client_init",
    "send_client_login",
]
