"""Length-prefixed framing for the post-probe Quassel stream.

After the probe handshake completes (and TLS, if negotiated), every message
between client and core is wrapped in a 4-byte big-endian length prefix
followed by `length` bytes of payload. This is the same shape Qt uses with
`QDataStream::writeBytes` / `readBytes` and it's how DataStreamPeer's
`writeMessage(QByteArray)` puts bytes on the wire.

We hard-cap the per-frame length to bound attacker-controlled allocations
identically to the way `QDataStreamReader` does for inner length prefixes.
The default 64 MiB matches the largest legitimate `Message` blob a busy
backlog response could plausibly produce.
"""

from __future__ import annotations

import asyncio
import struct

from quasseltui.protocol.errors import ConnectionClosed, QuasselError

DEFAULT_MAX_FRAME_BYTES = 64 * 1024 * 1024  # 64 MiB

_HEADER_FMT = ">I"
_HEADER_SIZE = 4


class FrameTooLargeError(QuasselError):
    """A peer-supplied frame length exceeds the configured cap."""


def encode_frame(payload: bytes) -> bytes:
    """Prepend the 4-byte big-endian length prefix to `payload`."""
    return struct.pack(_HEADER_FMT, len(payload)) + payload


def parse_frame_header(header: bytes) -> int:
    """Decode a 4-byte big-endian length prefix into an int."""
    if len(header) != _HEADER_SIZE:
        raise QuasselError(f"frame header must be {_HEADER_SIZE} bytes, got {len(header)}")
    (length,) = struct.unpack(_HEADER_FMT, header)
    return int(length)


async def read_frame(
    reader: asyncio.StreamReader,
    *,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> bytes:
    """Read one length-prefixed frame from the stream.

    Raises `ConnectionClosed` if EOF arrives before the header or payload is
    complete, and `FrameTooLargeError` if the peer announces a payload larger
    than `max_frame_bytes` — the latter is checked BEFORE we start reading
    the payload so a malicious peer can't make us allocate gigabytes.
    """
    header = await _read_exactly(reader, _HEADER_SIZE)
    length = parse_frame_header(header)
    if length > max_frame_bytes:
        raise FrameTooLargeError(f"frame length {length} exceeds max_frame_bytes {max_frame_bytes}")
    if length == 0:
        return b""
    return await _read_exactly(reader, length)


async def write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    """Write one length-prefixed frame and drain the buffer."""
    writer.write(encode_frame(payload))
    await writer.drain()


async def _read_exactly(reader: asyncio.StreamReader, n: int) -> bytes:
    """`StreamReader.readexactly` but raises our typed `ConnectionClosed`."""
    try:
        return await reader.readexactly(n)
    except asyncio.IncompleteReadError as exc:
        raise ConnectionClosed(
            f"connection closed after {len(exc.partial)} of {n} expected bytes"
        ) from exc
