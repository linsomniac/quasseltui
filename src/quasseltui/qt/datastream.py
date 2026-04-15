"""Pure-Python QDataStream reader/writer for Qt binary serialization.

Wire format rules (matching Qt's QDataStream::Qt_5_0+ in big-endian mode):

- All integers are big-endian.
- Booleans are one byte (0 or 1).
- QString is a `quint32` byte length (NOT character count) followed by raw
  UTF-16BE code units. The sentinel `0xFFFFFFFF` represents a null QString.
  Qt's `QString` is a sequence of 16-bit code units with no enforced surrogate
  pairing, so we use Python's `surrogatepass` error handler to round-trip lone
  surrogates rather than rejecting them as the strict UTF-16 codec would.
- QByteArray uses the same shape but raw bytes; `0xFFFFFFFF` represents null.

Quassel always uses big-endian, version-stable wire format, so we hard-code that
here rather than tracking QDataStream version negotiation.

The reader takes configurable size limits to bound attacker-controlled length
prefixes — without them a malformed core message could ask us to allocate
multi-gigabyte strings or byte arrays. The defaults are conservative for an
IRC client; raise them per-instance if you actually need to decode something
larger.
"""

from __future__ import annotations

import struct

DEFAULT_MAX_STRING_BYTES = 16 * 1024 * 1024  # 16 MiB
DEFAULT_MAX_BYTEARRAY_BYTES = 64 * 1024 * 1024  # 64 MiB
DEFAULT_MAX_CONTAINER_ITEMS = 1_000_000


class QDataStreamError(ValueError):
    """Raised when decoding fails (truncated buffer, bad encoding, limit hit)."""


_NULL_LEN = 0xFFFFFFFF


class QDataStreamReader:
    """Sequential reader over a bytes buffer, big-endian Qt primitives."""

    __slots__ = (
        "_data",
        "_pos",
        "max_bytearray_bytes",
        "max_container_items",
        "max_string_bytes",
    )

    def __init__(
        self,
        data: bytes,
        *,
        max_string_bytes: int = DEFAULT_MAX_STRING_BYTES,
        max_bytearray_bytes: int = DEFAULT_MAX_BYTEARRAY_BYTES,
        max_container_items: int = DEFAULT_MAX_CONTAINER_ITEMS,
    ) -> None:
        self._data = data
        self._pos = 0
        self.max_string_bytes = max_string_bytes
        self.max_bytearray_bytes = max_bytearray_bytes
        self.max_container_items = max_container_items

    @property
    def position(self) -> int:
        return self._pos

    def remaining(self) -> int:
        return len(self._data) - self._pos

    def at_end(self) -> bool:
        return self._pos >= len(self._data)

    def read_bytes(self, n: int) -> bytes:
        if n < 0:
            raise QDataStreamError(f"negative read length: {n}")
        end = self._pos + n
        if end > len(self._data):
            raise QDataStreamError(
                f"truncated buffer: wanted {n} bytes at offset {self._pos}, "
                f"only {len(self._data) - self._pos} available"
            )
        chunk = self._data[self._pos : end]
        self._pos = end
        return chunk

    def _unpack(self, fmt: str) -> tuple[int, ...]:
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self.read_bytes(size))

    def read_uint8(self) -> int:
        return self._unpack(">B")[0]

    def read_uint16(self) -> int:
        return self._unpack(">H")[0]

    def read_uint32(self) -> int:
        return self._unpack(">I")[0]

    def read_uint64(self) -> int:
        return self._unpack(">Q")[0]

    def read_int8(self) -> int:
        return self._unpack(">b")[0]

    def read_int16(self) -> int:
        return self._unpack(">h")[0]

    def read_int32(self) -> int:
        return self._unpack(">i")[0]

    def read_int64(self) -> int:
        return self._unpack(">q")[0]

    def read_bool(self) -> bool:
        return self.read_uint8() != 0

    def read_qstring(self) -> str | None:
        length = self.read_uint32()
        if length == _NULL_LEN:
            return None
        if length == 0:
            return ""
        if length > self.max_string_bytes:
            raise QDataStreamError(
                f"QString length {length} exceeds max_string_bytes {self.max_string_bytes}"
            )
        if length % 2 != 0:
            raise QDataStreamError(f"QString byte length {length} is not a multiple of 2 (UTF-16)")
        raw = self.read_bytes(length)
        try:
            return raw.decode("utf-16-be", errors="surrogatepass")
        except UnicodeDecodeError as exc:  # pragma: no cover - defensive
            raise QDataStreamError(f"invalid UTF-16BE in QString: {exc}") from exc

    def read_qbytearray(self) -> bytes | None:
        length = self.read_uint32()
        if length == _NULL_LEN:
            return None
        if length == 0:
            return b""
        if length > self.max_bytearray_bytes:
            raise QDataStreamError(
                f"QByteArray length {length} exceeds max_bytearray_bytes {self.max_bytearray_bytes}"
            )
        return self.read_bytes(length)


class QDataStreamWriter:
    """Sequential writer accumulating Qt-formatted big-endian bytes."""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def __len__(self) -> int:
        return len(self._buf)

    def to_bytes(self) -> bytes:
        return bytes(self._buf)

    def write_bytes(self, data: bytes) -> None:
        self._buf.extend(data)

    def _pack(self, fmt: str, value: int) -> None:
        self._buf.extend(struct.pack(fmt, value))

    def write_uint8(self, value: int) -> None:
        self._pack(">B", value)

    def write_uint16(self, value: int) -> None:
        self._pack(">H", value)

    def write_uint32(self, value: int) -> None:
        self._pack(">I", value)

    def write_uint64(self, value: int) -> None:
        self._pack(">Q", value)

    def write_int8(self, value: int) -> None:
        self._pack(">b", value)

    def write_int16(self, value: int) -> None:
        self._pack(">h", value)

    def write_int32(self, value: int) -> None:
        self._pack(">i", value)

    def write_int64(self, value: int) -> None:
        self._pack(">q", value)

    def write_bool(self, value: bool) -> None:
        self.write_uint8(1 if value else 0)

    def write_qstring(self, value: str | None) -> None:
        if value is None:
            self.write_uint32(_NULL_LEN)
            return
        encoded = value.encode("utf-16-be", errors="surrogatepass")
        self.write_uint32(len(encoded))
        self._buf.extend(encoded)

    def write_qbytearray(self, value: bytes | None) -> None:
        if value is None:
            self.write_uint32(_NULL_LEN)
            return
        self.write_uint32(len(value))
        self._buf.extend(value)
