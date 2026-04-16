"""SignalProxy message codec — the CONNECTED-state framing.

Once the handshake is done, every framed `QVariantList` carries one of six
SignalProxy message kinds. The first element is a `qint16` discriminator
(serialized via QVariant<Short>); the remaining elements depend on the kind.
The wire format is taken straight from
`src/common/protocols/datastream/datastreampeer.cpp::handlePackedFunc` and
the matching `dispatch()` overloads — see the verbatim quotes below for each
case.

Discriminator values from `datastreampeer.h::RequestType`:

    Sync           = 1
    RpcCall        = 2
    InitRequest    = 3
    InitData       = 4
    HeartBeat      = 5
    HeartBeatReply = 6

Note that the discriminator is `qint16`, NOT `qint32` — so the QVariant
envelope has type id `Quassel::Types::VariantType::Short = 130` (which we
expose as `QMetaType.Short` for muscle memory). This is the only place the
distinction matters; everything else uses standard Qt-flavored type IDs.

Defensive parsing: malformed messages raise `SignalProxyError` but never
attempt to "skip" past garbled payloads — desynchronization here is
unrecoverable, so we surface the failure to the connection state machine
which closes the socket. Forward-compatible parsing (unknown class names,
unknown slot names) is the responsibility of the layer above; this codec
just unpacks bytes into typed structs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from quasseltui.protocol.errors import QuasselError
from quasseltui.qt.datastream import QDataStreamReader, QDataStreamWriter
from quasseltui.qt.types import QMetaType
from quasseltui.qt.variant import (
    read_qvariantlist,
    write_variant,
)


class SignalProxyError(QuasselError):
    """Raised when a SignalProxy frame doesn't match any known shape."""


# ---------------------------------------------------------------------------
# RequestType discriminator. Using IntEnum-without-the-class for ergonomics —
# these values appear in match statements and exception messages, and we
# never need to iterate them.
# ---------------------------------------------------------------------------


REQUEST_SYNC = 1
REQUEST_RPC_CALL = 2
REQUEST_INIT_REQUEST = 3
REQUEST_INIT_DATA = 4
REQUEST_HEARTBEAT = 5
REQUEST_HEARTBEAT_REPLY = 6

REQUEST_NAMES: dict[int, str] = {
    REQUEST_SYNC: "Sync",
    REQUEST_RPC_CALL: "RpcCall",
    REQUEST_INIT_REQUEST: "InitRequest",
    REQUEST_INIT_DATA: "InitData",
    REQUEST_HEARTBEAT: "HeartBeat",
    REQUEST_HEARTBEAT_REPLY: "HeartBeatReply",
}


# ---------------------------------------------------------------------------
# Per-kind dataclasses. Names mirror `Protocol::SyncMessage`,
# `Protocol::RpcCall`, etc. from `src/common/protocol.h`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyncMessage:
    """Cross-peer slot invocation on a SyncObject.

    `class_name` and `slot_name` are bytes (Quassel emits them as
    `QByteArray`). `object_name` is a `str` because it's an actual object
    identifier — Quassel emits it as `objectName.toUtf8()` and the C++ side
    decodes back via `QString::fromUtf8`. Following the C++ semantics here
    means downstream code can match on `class_name == b"Network"` rather
    than dealing with encoding ambiguity.
    """

    class_name: bytes
    object_name: str
    slot_name: bytes
    params: list[Any] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RpcCall:
    """Top-level signal invocation (no SyncObject involved).

    `signal_name` is bytes for the same reason as `class_name` above.
    Quassel uses signal-style strings prefixed with the Qt metacall
    digit (e.g. `b"2sendInput(BufferInfo,QString)"`).
    """

    signal_name: bytes
    params: list[Any] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class InitRequest:
    """Request the full initial state for `(class_name, object_name)`."""

    class_name: bytes
    object_name: str


@dataclass(frozen=True, slots=True)
class InitData:
    """Reply to InitRequest. `init_data` is a flat property map.

    The wire encodes this as alternating `(QByteArray, QVariant)` pairs in
    the trailing QVariantList; we re-pack into a `dict[str, Any]` here so
    callers don't have to worry about the wire shape.
    """

    class_name: bytes
    object_name: str
    init_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HeartBeat:
    """Periodic keepalive. Reply with `HeartBeatReply` carrying the same ts."""

    timestamp: datetime


@dataclass(frozen=True, slots=True)
class HeartBeatReply:
    """Server's reply to our HeartBeat (or our reply to its HeartBeat)."""

    timestamp: datetime


SignalProxyMessage = SyncMessage | RpcCall | InitRequest | InitData | HeartBeat | HeartBeatReply


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


def decode_signalproxy_payload(
    payload: bytes,
    *,
    peer_features: frozenset[str] = frozenset(),
) -> SignalProxyMessage:
    """Decode one CONNECTED-state framed payload into a SignalProxy message.

    `payload` is the bytes inside one `read_frame` result — i.e. the
    QVariantList with no length prefix. `peer_features` is the negotiated
    feature set, which the Message user-type codec needs to know how to
    parse trailing fields.

    The reader must be fully consumed by the time we return. Trailing bytes
    after the top-level QVariantList are a protocol error — framing keeps
    the next message in sync regardless, so we wouldn't desynchronize the
    whole stream, but "extra bytes after a decoded payload" still means the
    core sent something we don't understand and we'd rather fail loudly
    than silently drop the tail.
    """
    reader = QDataStreamReader(payload, peer_features=peer_features)
    items = read_qvariantlist(reader)
    if not reader.at_end():
        raise SignalProxyError(
            f"trailing {reader.remaining()} bytes after SignalProxy QVariantList"
        )
    if not items:
        raise SignalProxyError("empty SignalProxy frame (zero-element QVariantList)")

    discriminator = items[0]
    if not isinstance(discriminator, int) or isinstance(discriminator, bool):
        raise SignalProxyError(
            f"first SignalProxy element must be an int discriminator, "
            f"got {type(discriminator).__name__}"
        )

    rest = items[1:]
    if discriminator == REQUEST_SYNC:
        return _decode_sync(rest)
    if discriminator == REQUEST_RPC_CALL:
        return _decode_rpc_call(rest)
    if discriminator == REQUEST_INIT_REQUEST:
        return _decode_init_request(rest)
    if discriminator == REQUEST_INIT_DATA:
        return _decode_init_data(rest)
    if discriminator == REQUEST_HEARTBEAT:
        return _decode_heartbeat(rest, reply=False)
    if discriminator == REQUEST_HEARTBEAT_REPLY:
        return _decode_heartbeat(rest, reply=True)
    raise SignalProxyError(f"unknown SignalProxy discriminator {discriminator}")


def _expect_bytes(value: Any, field_name: str, kind: str) -> bytes:
    if not isinstance(value, bytes | bytearray):
        raise SignalProxyError(
            f"{kind}: expected QByteArray for {field_name}, got {type(value).__name__}"
        )
    return bytes(value)


def _bytes_to_object_name(value: Any, kind: str, field_name: str) -> str:
    """Decode an `objectName.toUtf8()` field back into a Python str.

    The C++ side does `QString::fromUtf8(params.takeFirst().toByteArray())`
    so we do the same. Any malformed UTF-8 in an object name is much more
    likely a protocol confusion than a real Unicode oddity, so we use
    `errors="replace"` and let the surrounding code see the placeholder.

    A null QByteArray (length sentinel 0xFFFFFFFF) is treated as the
    empty string — `QString::fromUtf8(QByteArray())` returns `""` in
    Qt, and some Quassel cores encode singleton object names (like
    BufferSyncer's `""`) as null rather than empty on the wire.
    """
    # AIDEV-NOTE: Null QByteArray handling — some Quassel cores send null
    # instead of empty for singleton objectNames. Without this, the
    # connection crashes on the first Sync message after SessionInit.
    if value is None:
        return ""
    raw = _expect_bytes(value, field_name, kind)
    return raw.decode("utf-8", errors="replace")


def _decode_sync(rest: list[Any]) -> SyncMessage:
    if len(rest) < 3:
        raise SignalProxyError(
            f"Sync: needs at least className/objectName/slotName, got {len(rest)} items"
        )
    class_name = _expect_bytes(rest[0], "className", "Sync")
    object_name = _bytes_to_object_name(rest[1], "Sync", "objectName")
    slot_name = _expect_bytes(rest[2], "slotName", "Sync")
    params = rest[3:]
    return SyncMessage(
        class_name=class_name,
        object_name=object_name,
        slot_name=slot_name,
        params=params,
    )


def _decode_rpc_call(rest: list[Any]) -> RpcCall:
    if len(rest) < 1:
        raise SignalProxyError("RpcCall: needs at least a signalName")
    signal_name = _expect_bytes(rest[0], "signalName", "RpcCall")
    params = rest[1:]
    return RpcCall(signal_name=signal_name, params=params)


def _decode_init_request(rest: list[Any]) -> InitRequest:
    if len(rest) != 2:
        raise SignalProxyError(
            f"InitRequest: needs exactly className/objectName, got {len(rest)} items"
        )
    class_name = _expect_bytes(rest[0], "className", "InitRequest")
    object_name = _bytes_to_object_name(rest[1], "InitRequest", "objectName")
    return InitRequest(class_name=class_name, object_name=object_name)


def _decode_init_data(rest: list[Any]) -> InitData:
    if len(rest) < 2:
        raise SignalProxyError(
            f"InitData: needs at least className/objectName, got {len(rest)} items"
        )
    class_name = _expect_bytes(rest[0], "className", "InitData")
    object_name = _bytes_to_object_name(rest[1], "InitData", "objectName")
    kv_pairs = rest[2:]
    if len(kv_pairs) % 2 != 0:
        raise SignalProxyError(
            f"InitData: trailing key/value pairs must be even, got {len(kv_pairs)} items"
        )
    init_data: dict[str, Any] = {}
    for i in range(0, len(kv_pairs), 2):
        key_raw = _expect_bytes(kv_pairs[i], f"key#{i // 2}", "InitData")
        key = key_raw.decode("utf-8", errors="replace")
        init_data[key] = kv_pairs[i + 1]
    return InitData(class_name=class_name, object_name=object_name, init_data=init_data)


def _decode_heartbeat(rest: list[Any], *, reply: bool) -> HeartBeat | HeartBeatReply:
    kind = "HeartBeatReply" if reply else "HeartBeat"
    if len(rest) != 1:
        raise SignalProxyError(f"{kind}: needs exactly a timestamp, got {len(rest)} items")
    ts = rest[0]
    if not isinstance(ts, datetime):
        raise SignalProxyError(f"{kind}: expected QDateTime timestamp, got {type(ts).__name__}")
    return HeartBeatReply(timestamp=ts) if reply else HeartBeat(timestamp=ts)


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


def encode_signalproxy_payload(
    message: SignalProxyMessage,
    *,
    peer_features: frozenset[str] = frozenset(),
) -> bytes:
    """Encode a SignalProxy message back into framed-payload bytes.

    The result is the body that goes into `write_frame(...)` — it does
    NOT include the 4-byte length prefix (the framing layer adds that).
    """
    writer = QDataStreamWriter(peer_features=peer_features)

    if isinstance(message, SyncMessage):
        items: list[Any] = [
            _DiscriminatorTag(REQUEST_SYNC),
            message.class_name,
            message.object_name.encode("utf-8"),
            message.slot_name,
            *message.params,
        ]
    elif isinstance(message, RpcCall):
        items = [
            _DiscriminatorTag(REQUEST_RPC_CALL),
            message.signal_name,
            *message.params,
        ]
    elif isinstance(message, InitRequest):
        items = [
            _DiscriminatorTag(REQUEST_INIT_REQUEST),
            message.class_name,
            message.object_name.encode("utf-8"),
        ]
    elif isinstance(message, InitData):
        flat: list[Any] = []
        for key, value in message.init_data.items():
            flat.append(key.encode("utf-8"))
            flat.append(value)
        items = [
            _DiscriminatorTag(REQUEST_INIT_DATA),
            message.class_name,
            message.object_name.encode("utf-8"),
            *flat,
        ]
    elif isinstance(message, HeartBeat):
        items = [_DiscriminatorTag(REQUEST_HEARTBEAT), message.timestamp]
    elif isinstance(message, HeartBeatReply):
        items = [_DiscriminatorTag(REQUEST_HEARTBEAT_REPLY), message.timestamp]
    else:  # pragma: no cover - exhaustive match above
        raise SignalProxyError(f"unknown SignalProxy message type {type(message).__name__}")

    _write_signalproxy_list(writer, items)
    return writer.to_bytes()


class _DiscriminatorTag:
    """Marker so `write_variant` writes an int as `Short` not `Int`.

    Quassel's `dispatch()` writes the discriminator as `(qint16)Sync` etc.,
    which lands on the wire with type id `VariantType::Short = 130`. Our
    inferring `write_variant(int)` defaults to `Int = 2` — a real Quassel
    core would reject the resulting envelope as the wrong type for the
    first element. We use this tag to force the codec to pick `Short` for
    just this slot without touching the type-inference rules everywhere
    else.
    """

    __slots__ = ("value",)

    def __init__(self, value: int) -> None:
        self.value = value


def _write_signalproxy_list(writer: QDataStreamWriter, items: list[Any]) -> None:
    """Write a SignalProxy QVariantList where item[0] is a Short tag."""
    writer.write_uint32(len(items))
    for i, item in enumerate(items):
        if i == 0 and isinstance(item, _DiscriminatorTag):
            write_variant(writer, item.value, type_id=QMetaType.Short)
        else:
            write_variant(writer, item)


__all__ = [
    "REQUEST_HEARTBEAT",
    "REQUEST_HEARTBEAT_REPLY",
    "REQUEST_INIT_DATA",
    "REQUEST_INIT_REQUEST",
    "REQUEST_NAMES",
    "REQUEST_RPC_CALL",
    "REQUEST_SYNC",
    "HeartBeat",
    "HeartBeatReply",
    "InitData",
    "InitRequest",
    "RpcCall",
    "SignalProxyError",
    "SignalProxyMessage",
    "SyncMessage",
    "decode_signalproxy_payload",
    "encode_signalproxy_payload",
]
