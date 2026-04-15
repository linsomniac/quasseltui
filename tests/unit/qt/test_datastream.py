"""Unit tests for the low-level QDataStream reader/writer.

These tests guard the primitive bit-bashing layer. Most importantly they pin
the exact wire format of QString (byte length, NOT character count, with
0xFFFFFFFF as the null sentinel) — the single most common source of decoder
bugs in Qt-binary-format clients.
"""

from __future__ import annotations

import pytest

from quasseltui.qt.datastream import QDataStreamError, QDataStreamReader, QDataStreamWriter


class TestPrimitiveRoundtrips:
    @pytest.mark.parametrize(
        ("value", "writer_method", "reader_method"),
        [
            (0, "write_uint8", "read_uint8"),
            (255, "write_uint8", "read_uint8"),
            (65535, "write_uint16", "read_uint16"),
            (0xDEADBEEF, "write_uint32", "read_uint32"),
            (0xDEADBEEFCAFEBABE, "write_uint64", "read_uint64"),
            (-128, "write_int8", "read_int8"),
            (127, "write_int8", "read_int8"),
            (-32768, "write_int16", "read_int16"),
            (-2_000_000_000, "write_int32", "read_int32"),
            (-(2**62), "write_int64", "read_int64"),
        ],
    )
    def test_integer_roundtrip(
        self,
        value: int,
        writer_method: str,
        reader_method: str,
    ) -> None:
        writer = QDataStreamWriter()
        getattr(writer, writer_method)(value)
        reader = QDataStreamReader(writer.to_bytes())
        assert getattr(reader, reader_method)() == value
        assert reader.at_end()

    def test_bool_roundtrip(self) -> None:
        writer = QDataStreamWriter()
        writer.write_bool(True)
        writer.write_bool(False)
        reader = QDataStreamReader(writer.to_bytes())
        assert reader.read_bool() is True
        assert reader.read_bool() is False

    def test_uint32_is_big_endian(self) -> None:
        writer = QDataStreamWriter()
        writer.write_uint32(0x01020304)
        assert writer.to_bytes() == b"\x01\x02\x03\x04"


class TestQStringWireFormat:
    """Pin the exact QString wire format. Do NOT change these without proof."""

    def test_hello_is_known_blob(self) -> None:
        """QString('Hello') is 4-byte BE byte-length + 10 UTF-16BE bytes."""
        writer = QDataStreamWriter()
        writer.write_qstring("Hello")
        expected = (
            b"\x00\x00\x00\x0a"  # length = 10 bytes (NOT 5 characters)
            b"\x00H\x00e\x00l\x00l\x00o"
        )
        assert writer.to_bytes() == expected

    def test_null_qstring_is_ff_ff_ff_ff(self) -> None:
        writer = QDataStreamWriter()
        writer.write_qstring(None)
        assert writer.to_bytes() == b"\xff\xff\xff\xff"
        assert QDataStreamReader(writer.to_bytes()).read_qstring() is None

    def test_empty_qstring_is_zero_length(self) -> None:
        writer = QDataStreamWriter()
        writer.write_qstring("")
        assert writer.to_bytes() == b"\x00\x00\x00\x00"
        assert QDataStreamReader(writer.to_bytes()).read_qstring() == ""

    @pytest.mark.parametrize(
        "text",
        ["Hello", "", "ascii only", "naïve", "日本語", "🎉 emoji 🎉", "mixed: αβγ Ω"],
    )
    def test_qstring_roundtrip(self, text: str) -> None:
        writer = QDataStreamWriter()
        writer.write_qstring(text)
        reader = QDataStreamReader(writer.to_bytes())
        assert reader.read_qstring() == text
        assert reader.at_end()

    def test_qstring_byte_length_not_char_length(self) -> None:
        """Each non-BMP char is 2 UTF-16 code units = 4 bytes; BMP chars are 2 bytes."""
        text = "ab"  # 2 chars, 4 bytes
        writer = QDataStreamWriter()
        writer.write_qstring(text)
        # 4-byte length prefix = 4
        assert writer.to_bytes()[:4] == b"\x00\x00\x00\x04"

    def test_qstring_decode_rejects_odd_length(self) -> None:
        # Length prefix 3 (odd) is invalid for UTF-16BE.
        bad = b"\x00\x00\x00\x03\x00\x48\x00"
        with pytest.raises(QDataStreamError, match="multiple of 2"):
            QDataStreamReader(bad).read_qstring()


class TestQByteArray:
    def test_empty(self) -> None:
        writer = QDataStreamWriter()
        writer.write_qbytearray(b"")
        assert writer.to_bytes() == b"\x00\x00\x00\x00"
        assert QDataStreamReader(writer.to_bytes()).read_qbytearray() == b""

    def test_null(self) -> None:
        writer = QDataStreamWriter()
        writer.write_qbytearray(None)
        assert writer.to_bytes() == b"\xff\xff\xff\xff"
        assert QDataStreamReader(writer.to_bytes()).read_qbytearray() is None

    def test_roundtrip_with_binary(self) -> None:
        payload = bytes(range(256))
        writer = QDataStreamWriter()
        writer.write_qbytearray(payload)
        reader = QDataStreamReader(writer.to_bytes())
        assert reader.read_qbytearray() == payload
        assert reader.at_end()


class TestReaderBoundaries:
    def test_read_past_end_raises(self) -> None:
        reader = QDataStreamReader(b"\x01\x02")
        with pytest.raises(QDataStreamError, match="truncated"):
            reader.read_uint32()

    def test_negative_read_raises(self) -> None:
        reader = QDataStreamReader(b"")
        with pytest.raises(QDataStreamError, match="negative"):
            reader.read_bytes(-1)

    def test_remaining_and_position(self) -> None:
        reader = QDataStreamReader(b"\x00\x00\x00\x05extra")
        assert reader.remaining() == 9
        assert reader.read_uint32() == 5
        assert reader.position == 4
        assert reader.remaining() == 5
        assert reader.read_bytes(5) == b"extra"
        assert reader.at_end()
