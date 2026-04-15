"""Quassel probe handshake — the unframed byte exchange before TLS.

This is the very first thing that happens on a fresh socket. The client
announces what protocols/features it supports, the core picks one, and if
TLS was offered and accepted both sides flip the same socket into TLS
without exchanging any further plaintext.

Wire format (all big-endian quint32, no framing):

    Client → Core:
        magic   = 0x42b33f00 | client_features              # 4 bytes
        proto_1 = type | (proto_features << 8)               # 4 bytes
        ...
        proto_N = type | (proto_features << 8) | 0x80000000  # 4 bytes (last)

    Core → Client:
        reply = chosen_type | (peer_features << 8) | (conn_features << 24)

Where:
    - `client_features` is a bitfield of `Encryption (0x01)` and
      `Compression (0x02)` — the connection-level features we'd be willing
      to use if the core supports them.
    - Each protocol entry packs the protocol kind (`DataStream = 0x02`) in
      the low byte and the protocol-specific feature bits in bits 8-23.
      The high bit (`0x80000000`) marks the last entry in the list.
    - `conn_features` in the reply is the connection-level features the
      core actually enabled — the intersection of what we offered and what
      the core supports. If `Encryption` is set, both sides MUST upgrade
      the socket to TLS immediately after this 4-byte reply, with no
      further plaintext.

We never offer Compression. Quassel's compression layer is an optional zlib
wrap that we'd have to implement on top of the framing layer; it's not on
the critical path for v1 and saves a class of subtle bugs.

Source of truth: `clientauthhandler.cpp::onSocketConnected` and
`coreauthhandler.cpp::peekClientHeader` in the Quassel C++ tree.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag

from quasseltui.protocol.errors import ProbeError
from quasseltui.protocol.framing import _read_exactly

_QUASSEL_MAGIC = 0x42B33F00
_END_LIST_BIT = 0x80000000


class ProtocolType(IntEnum):
    """Quassel `Protocol::Type` enum from `protocol.h`.

    We only ever ask for `DataStream` — the legacy QVariantMap-based
    protocol is from the pre-2013 era and we have no reason to add the
    second codec just to support it.
    """

    Internal = 0x00
    Legacy = 0x01
    DataStream = 0x02


class ConnectionFeature(IntFlag):
    """Connection-level feature bits exchanged during the probe.

    These live in the `magic` low byte (offered) and the reply high byte
    (negotiated). They are distinct from the per-protocol feature bits and
    the `Features` field in `ClientInit`.
    """

    NONE = 0x00
    Encryption = 0x01
    Compression = 0x02


@dataclass(frozen=True, slots=True)
class NegotiatedProtocol:
    """The result of a successful probe.

    `tls_required` is the bit that tells the caller it MUST run a TLS
    upgrade on the same socket before any further bytes go either way. The
    caller — not this module — owns the SSL context.
    """

    protocol: ProtocolType
    peer_features: int
    connection_features: ConnectionFeature

    @property
    def tls_required(self) -> bool:
        return bool(self.connection_features & ConnectionFeature.Encryption)

    @property
    def compression_enabled(self) -> bool:
        return bool(self.connection_features & ConnectionFeature.Compression)


def build_probe_request(
    *,
    offered_features: ConnectionFeature = ConnectionFeature.Encryption,
    protocols: tuple[tuple[ProtocolType, int], ...] = ((ProtocolType.DataStream, 0),),
) -> bytes:
    """Build the bytes a client emits to start the probe handshake.

    `offered_features` is what we're willing to use connection-level. We
    default to offering Encryption only — the core will reply with a subset.

    `protocols` is the ordered list of `(type, per-protocol-feature-bits)`
    pairs we support. The order matters: the core picks the first one in our
    list that it can also speak, so put the preferred protocol first. We
    only ever ship DataStream.
    """
    if not protocols:
        raise ValueError("at least one protocol must be offered")

    parts = [struct.pack(">I", _QUASSEL_MAGIC | int(offered_features))]
    last_index = len(protocols) - 1
    for i, (proto_type, proto_features) in enumerate(protocols):
        if not 0 <= proto_features <= 0xFFFF:
            raise ValueError(f"protocol features must fit in 16 bits, got {proto_features:#x}")
        entry = int(proto_type) | (proto_features << 8)
        if i == last_index:
            entry |= _END_LIST_BIT
        parts.append(struct.pack(">I", entry))
    return b"".join(parts)


def parse_probe_reply(reply: bytes) -> NegotiatedProtocol:
    """Decode the 4-byte server reply into a `NegotiatedProtocol`.

    Raises `ProbeError` on a wrong-length buffer or an unrecognized
    protocol type — both indicate the server is not actually a Quassel core
    at the version we expect.
    """
    if len(reply) != 4:
        raise ProbeError(f"probe reply must be 4 bytes, got {len(reply)}")
    word = struct.unpack(">I", reply)[0]
    proto_byte = word & 0xFF
    peer_features = (word >> 8) & 0xFFFF
    conn_features = (word >> 24) & 0xFF
    try:
        protocol = ProtocolType(proto_byte)
    except ValueError as exc:
        raise ProbeError(
            f"core selected unknown protocol type {proto_byte:#x}; we only speak DataStream"
        ) from exc
    if protocol == ProtocolType.Legacy:
        raise ProbeError("core selected the Legacy protocol — quasseltui only speaks DataStream")
    return NegotiatedProtocol(
        protocol=protocol,
        peer_features=peer_features,
        connection_features=ConnectionFeature(conn_features),
    )


async def probe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    offered_features: ConnectionFeature = ConnectionFeature.Encryption,
) -> NegotiatedProtocol:
    """Send the probe and read the reply on an open socket.

    The caller is responsible for opening the connection, and — if the
    returned `tls_required` is set — for performing the `start_tls` upgrade
    on this exact socket before any further reads or writes.
    """
    writer.write(build_probe_request(offered_features=offered_features))
    await writer.drain()
    reply = await _read_exactly(reader, 4)
    return parse_probe_reply(reply)
