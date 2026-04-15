"""Unit tests for the low-level QDataStream reader/writer.

These tests guard the primitive bit-bashing layer. Most importantly they pin
the exact wire format of QString (byte length, NOT character count, with
0xFFFFFFFF as the null sentinel) — the single most common source of decoder
bugs in Qt-binary-format clients.
"""

from __future__ import annotations

import datetime as dt

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


class TestQStringSurrogateAndNul:
    """Qt's QString is a sequence of 16-bit code units, not strict UTF-16 — it
    permits lone surrogates and embedded NULs. Our codec must round-trip both."""

    def test_lone_high_surrogate_roundtrip(self) -> None:
        # U+D83D alone is a lone high surrogate (no following low surrogate).
        text = "before\ud83dafter"
        writer = QDataStreamWriter()
        writer.write_qstring(text)
        reader = QDataStreamReader(writer.to_bytes())
        assert reader.read_qstring() == text
        assert reader.at_end()

    def test_lone_low_surrogate_roundtrip(self) -> None:
        text = "x\udce9y"  # U+DCE9 is a lone low surrogate
        writer = QDataStreamWriter()
        writer.write_qstring(text)
        reader = QDataStreamReader(writer.to_bytes())
        assert reader.read_qstring() == text

    def test_embedded_nul_roundtrip(self) -> None:
        text = "abc\x00def\x00\x00ghi"
        writer = QDataStreamWriter()
        writer.write_qstring(text)
        encoded = writer.to_bytes()
        # Length prefix should be 24 (12 BMP code units, 2 bytes each).
        assert encoded[:4] == b"\x00\x00\x00\x18"
        reader = QDataStreamReader(encoded)
        assert reader.read_qstring() == text


class TestLengthLimits:
    """Bound attacker-controlled length prefixes so a malformed core can't ask
    us to allocate gigabytes of memory."""

    def test_qstring_length_above_limit_rejected(self) -> None:
        # Length prefix says 1024 bytes, but the limit is 100.
        bad = b"\x00\x00\x04\x00" + b"\x00" * 1024
        reader = QDataStreamReader(bad, max_string_bytes=100)
        with pytest.raises(QDataStreamError, match="exceeds max_string_bytes"):
            reader.read_qstring()

    def test_qstring_length_at_limit_succeeds(self) -> None:
        # Exactly at the limit is fine.
        text = "ab"  # 4 bytes
        writer = QDataStreamWriter()
        writer.write_qstring(text)
        reader = QDataStreamReader(writer.to_bytes(), max_string_bytes=4)
        assert reader.read_qstring() == text

    def test_qstring_huge_length_rejected_before_alloc(self) -> None:
        # 0x7fffffff (~2GB) length prefix in a tiny buffer — without the limit
        # the decoder would call read_bytes(2GB) and only THEN notice it's
        # truncated, but with the limit we reject before even calling
        # read_bytes. Verify the error message names the limit, not truncation.
        bad = b"\x7f\xff\xff\xfe"  # length = 2147483646 (even, so passes parity)
        reader = QDataStreamReader(bad)  # default 16 MB limit
        with pytest.raises(QDataStreamError, match="exceeds max_string_bytes"):
            reader.read_qstring()

    def test_qbytearray_length_above_limit_rejected(self) -> None:
        bad = b"\x00\x00\x04\x00" + b"\x00" * 1024
        reader = QDataStreamReader(bad, max_bytearray_bytes=100)
        with pytest.raises(QDataStreamError, match="exceeds max_bytearray_bytes"):
            reader.read_qbytearray()

    def test_qbytearray_length_at_limit_succeeds(self) -> None:
        payload = b"\x01\x02\x03\x04"
        writer = QDataStreamWriter()
        writer.write_qbytearray(payload)
        reader = QDataStreamReader(writer.to_bytes(), max_bytearray_bytes=4)
        assert reader.read_qbytearray() == payload

    def test_truncated_qstring_payload_raises(self) -> None:
        # Length prefix says 10 bytes but only 4 are available.
        bad = b"\x00\x00\x00\x0a\x00H\x00e"
        with pytest.raises(QDataStreamError, match="truncated"):
            QDataStreamReader(bad).read_qstring()

    def test_truncated_qbytearray_payload_raises(self) -> None:
        bad = b"\x00\x00\x00\x10ab"  # says 16 bytes, only 2 available
        with pytest.raises(QDataStreamError, match="truncated"):
            QDataStreamReader(bad).read_qbytearray()


class TestQDateTimeWireFormat:
    """Pin the Qt 4 wire format for QDateTime: `quint32 jd, quint32 ms, bool`.

    These 9 bytes are what Quassel sends for `HeartBeat::timestamp`. Qt 5/6
    still produce this same shape under stream version `Qt_4_2`, which is
    what Quassel pins both ends to. If our codec ever drifts from this
    layout the HeartBeat reply path silently stops working — the core
    would drop the connection after ~30 seconds with no useful error, so
    we'd rather catch it here.
    """

    def test_known_blob_2026_04_14(self) -> None:
        """Pin one exact datetime to its 9-byte wire representation."""
        value = dt.datetime(2026, 4, 14, 12, 34, 56, 789_000, tzinfo=dt.UTC)
        writer = QDataStreamWriter()
        writer.write_qdatetime(value)
        # julian_day = date(2026,4,14).toordinal() + 1721425 = 0x00258dd9
        # ms = 12*3600000 + 34*60000 + 56*1000 + 789 = 0x02b32c95
        # is_utc = 1
        assert writer.to_bytes() == b"\x00\x25\x8d\xd9\x02\xb3\x2c\x95\x01"

    def test_utc_roundtrip_preserves_tzinfo(self) -> None:
        value = dt.datetime(2026, 4, 14, 12, 34, 56, 789_000, tzinfo=dt.UTC)
        writer = QDataStreamWriter()
        writer.write_qdatetime(value)
        reader = QDataStreamReader(writer.to_bytes())
        result = reader.read_qdatetime()
        assert result == value
        assert result.tzinfo is dt.UTC
        assert reader.at_end()

    def test_naive_roundtrip_stays_naive(self) -> None:
        value = dt.datetime(2026, 1, 1, 0, 0, 0)
        writer = QDataStreamWriter()
        writer.write_qdatetime(value)
        reader = QDataStreamReader(writer.to_bytes())
        result = reader.read_qdatetime()
        assert result.replace(tzinfo=None) == value
        assert result.tzinfo is None

    def test_zero_julian_day_does_not_crash(self) -> None:
        """Quassel occasionally emits 0/0/0 for 'no timestamp'.

        The Qt 4 format uses quint32 for julian day, so 0 is a valid on-wire
        value even though it's far outside Python's `datetime` range. We
        clamp to `date.min` rather than crashing the SignalProxy read loop.
        """
        blob = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        reader = QDataStreamReader(blob)
        result = reader.read_qdatetime()
        assert result.year == 1
        assert reader.at_end()

    @pytest.mark.parametrize(
        "moment",
        [
            dt.datetime(1970, 1, 1, 0, 0, 0, tzinfo=dt.UTC),
            dt.datetime(2000, 2, 29, 12, 0, 0, tzinfo=dt.UTC),  # leap day
            dt.datetime(2038, 1, 19, 3, 14, 7, tzinfo=dt.UTC),  # qint32 wrap
            dt.datetime(2100, 12, 31, 23, 59, 59, 999_000, tzinfo=dt.UTC),
        ],
    )
    def test_various_moments_roundtrip(self, moment: dt.datetime) -> None:
        writer = QDataStreamWriter()
        writer.write_qdatetime(moment)
        reader = QDataStreamReader(writer.to_bytes())
        assert reader.read_qdatetime() == moment

    def test_tz_aware_non_utc_is_converted_to_utc(self) -> None:
        """Tz-aware datetime in another zone should normalize to UTC on write."""
        eastern = dt.timezone(dt.timedelta(hours=-5))
        local = dt.datetime(2026, 4, 14, 7, 34, 56, 789_000, tzinfo=eastern)
        expected_utc = local.astimezone(dt.UTC)
        writer = QDataStreamWriter()
        writer.write_qdatetime(local)
        reader = QDataStreamReader(writer.to_bytes())
        result = reader.read_qdatetime()
        assert result == expected_utc


class TestPeerFeaturesAttribute:
    """The Message user-type codec keys off this attribute on both Reader
    and Writer. Make sure the defaults don't surprise anyone and the
    explicit value flows through unchanged."""

    def test_reader_default_empty(self) -> None:
        assert QDataStreamReader(b"").peer_features == frozenset()

    def test_writer_default_empty(self) -> None:
        assert QDataStreamWriter().peer_features == frozenset()

    def test_reader_explicit_features(self) -> None:
        features = frozenset({"LongTime", "RichMessages"})
        reader = QDataStreamReader(b"", peer_features=features)
        assert reader.peer_features == features

    def test_writer_explicit_features(self) -> None:
        features = frozenset({"SenderPrefixes"})
        writer = QDataStreamWriter(peer_features=features)
        assert writer.peer_features == features
