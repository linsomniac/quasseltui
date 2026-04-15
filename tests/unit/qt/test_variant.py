"""Unit tests for QVariant read/write dispatch and container codecs."""

from __future__ import annotations

import pytest

from quasseltui.qt.datastream import QDataStreamError, QDataStreamReader, QDataStreamWriter
from quasseltui.qt.types import QMetaType
from quasseltui.qt.variant import (
    read_qvariantlist,
    read_qvariantmap,
    read_variant,
    write_qvariantlist,
    write_qvariantmap,
    write_variant,
)


def _roundtrip(value: object, type_id: int | None = None) -> object:
    writer = QDataStreamWriter()
    write_variant(writer, value, type_id=type_id)
    reader = QDataStreamReader(writer.to_bytes())
    out = read_variant(reader)
    assert reader.at_end()
    return out


class TestVariantPrimitives:
    @pytest.mark.parametrize(
        ("value", "type_id"),
        [
            (True, QMetaType.Bool),
            (False, QMetaType.Bool),
            (0, QMetaType.Int),
            (-1, QMetaType.Int),
            (2_000_000_000, QMetaType.Int),
            (4_000_000_000, QMetaType.UInt),
            (10**12, QMetaType.LongLong),
            (10**18, QMetaType.ULongLong),
            ("hello", QMetaType.QString),
            ("", QMetaType.QString),
            ("日本語", QMetaType.QString),
            (b"\x00\x01\x02", QMetaType.QByteArray),
            (b"", QMetaType.QByteArray),
        ],
    )
    def test_explicit_type_id(self, value: object, type_id: int) -> None:
        assert _roundtrip(value, type_id=type_id) == value

    def test_inferred_types(self) -> None:
        assert _roundtrip(True) is True
        assert _roundtrip(42) == 42
        assert _roundtrip("hi") == "hi"
        assert _roundtrip(b"bytes") == b"bytes"

    def test_none_serializes_as_invalid_variant(self) -> None:
        writer = QDataStreamWriter()
        write_variant(writer, None)
        # Invalid type (0) + null flag (1) = 5 bytes total
        assert writer.to_bytes() == b"\x00\x00\x00\x00\x01"
        assert read_variant(QDataStreamReader(writer.to_bytes())) is None

    def test_bool_inference_beats_int(self) -> None:
        """`bool` is an `int` subclass in Python — make sure we pick QMetaType.Bool."""
        writer = QDataStreamWriter()
        write_variant(writer, True)
        # First 4 bytes are the type ID — must be Bool (1), not Int (2).
        assert writer.to_bytes()[:4] == b"\x00\x00\x00\x01"

    def test_unsupported_type_raises(self) -> None:
        bad = b"\x00\x00\x00\xff\x00"  # type id 255, not registered
        with pytest.raises(QDataStreamError, match="unsupported QVariant type"):
            read_variant(QDataStreamReader(bad))


class TestQVariantList:
    def test_empty_list(self) -> None:
        writer = QDataStreamWriter()
        write_qvariantlist(writer, [])
        assert writer.to_bytes() == b"\x00\x00\x00\x00"
        reader = QDataStreamReader(writer.to_bytes())
        assert read_qvariantlist(reader) == []

    def test_mixed_types(self) -> None:
        original: list[object] = [True, 42, "hello", b"bytes", [1, 2, 3], {"k": "v"}]
        result = _roundtrip(original)
        assert result == original


class TestQVariantMap:
    def test_empty_map(self) -> None:
        writer = QDataStreamWriter()
        write_qvariantmap(writer, {})
        assert writer.to_bytes() == b"\x00\x00\x00\x00"
        reader = QDataStreamReader(writer.to_bytes())
        assert read_qvariantmap(reader) == {}

    def test_client_init_shaped_map(self) -> None:
        """Mirror the actual structure of a ClientInit message — the smoke test
        for Phase 2's first real-core handshake."""
        original = {
            "MsgType": "ClientInit",
            "ClientVersion": "quasseltui v0.0.0",
            "ClientDate": "2026-04-14",
            "Features": 0xC03F,
            "FeatureList": ["SynchronizedMarkerLine", "SaslAuthentication"],
        }
        result = _roundtrip(original)
        assert result == original

    def test_nested_maps(self) -> None:
        original = {
            "outer": {
                "inner": {
                    "leaf": "value",
                    "n": 7,
                },
            },
        }
        result = _roundtrip(original)
        assert result == original

    def test_roundtrip_via_explicit_writers(self) -> None:
        original = {"a": 1, "b": "two", "c": [True, False]}
        writer = QDataStreamWriter()
        write_qvariantmap(writer, original)
        reader = QDataStreamReader(writer.to_bytes())
        assert read_qvariantmap(reader) == original
        assert reader.at_end()


class TestTypedNullVariants:
    """Qt's QVariant::save always writes the typed payload after the null flag,
    even when is_null is set. If our decoder stops at the null flag, the next
    field reads garbage. These tests pin the consume-payload behavior.
    """

    def test_typed_null_qstring_followed_by_int(self) -> None:
        # Hand-built bytes:
        #   QVariant<QString>(null) = type=10, is_null=1, payload=0xFFFFFFFF
        #   QVariant<Int>(7)        = type=2,  is_null=0, payload=0x00000007
        blob = (
            b"\x00\x00\x00\x0a\x01\xff\xff\xff\xff"  # null QString variant
            b"\x00\x00\x00\x02\x00\x00\x00\x00\x07"  # Int(7) variant
        )
        reader = QDataStreamReader(blob)
        first = read_variant(reader)
        second = read_variant(reader)
        assert first is None
        assert second == 7
        assert reader.at_end()

    def test_typed_null_int_followed_by_qstring(self) -> None:
        # A null Int still has a 4-byte payload (the default-constructed value).
        # The reader must consume it before moving on.
        blob = (
            b"\x00\x00\x00\x02\x01\x00\x00\x00\x00"  # null Int variant
            b"\x00\x00\x00\x0a\x00\x00\x00\x00\x02\x00H"  # "H" QString variant
        )
        reader = QDataStreamReader(blob)
        first = read_variant(reader)
        second = read_variant(reader)
        assert first is None
        assert second == "H"
        assert reader.at_end()

    def test_typed_null_qbytearray_followed_by_bool(self) -> None:
        blob = (
            b"\x00\x00\x00\x0c\x01\xff\xff\xff\xff"  # null QByteArray variant
            b"\x00\x00\x00\x01\x00\x01"  # Bool(True) variant
        )
        reader = QDataStreamReader(blob)
        assert read_variant(reader) is None
        assert read_variant(reader) is True
        assert reader.at_end()

    def test_invalid_variant_has_no_payload(self) -> None:
        # Type Invalid (0) followed by null flag, no payload.
        blob = b"\x00\x00\x00\x00\x01" + b"\x00\x00\x00\x02\x00\x00\x00\x00\x09"
        reader = QDataStreamReader(blob)
        assert read_variant(reader) is None
        assert read_variant(reader) == 9
        assert reader.at_end()

    def test_write_typed_null_rejected(self) -> None:
        writer = QDataStreamWriter()
        with pytest.raises(QDataStreamError, match="typed-null QVariant writes are not supported"):
            write_variant(writer, None, type_id=QMetaType.QString)

    def test_write_invalid_variant_still_works(self) -> None:
        writer = QDataStreamWriter()
        write_variant(writer, None)
        assert writer.to_bytes() == b"\x00\x00\x00\x00\x01"


class TestContainerLimits:
    """Container counts come from the wire and must be bounded so a malformed
    core can't ask us to allocate millions of dict slots."""

    def test_qvariantlist_count_above_limit_rejected(self) -> None:
        # count = 100, limit = 5
        blob = b"\x00\x00\x00\x64"
        reader = QDataStreamReader(blob, max_container_items=5)
        with pytest.raises(QDataStreamError, match=r"QVariantList count.*exceeds"):
            from quasseltui.qt.variant import read_qvariantlist as _read

            _read(reader)

    def test_qvariantmap_count_above_limit_rejected(self) -> None:
        blob = b"\x00\x00\x00\x64"
        reader = QDataStreamReader(blob, max_container_items=5)
        with pytest.raises(QDataStreamError, match=r"QVariantMap count.*exceeds"):
            from quasseltui.qt.variant import read_qvariantmap as _read

            _read(reader)

    def test_qstringlist_count_above_limit_rejected(self) -> None:
        blob = b"\x00\x00\x00\x64"
        reader = QDataStreamReader(blob, max_container_items=5)
        with pytest.raises(QDataStreamError, match=r"QStringList count.*exceeds"):
            from quasseltui.qt.variant import read_qstringlist as _read

            _read(reader)

    def test_huge_count_rejected_before_alloc(self) -> None:
        # ~4 billion items would happily try to allocate a 4-billion-element
        # list before failing on truncation; the limit should reject first.
        blob = b"\xff\xff\xff\x00"  # count = 0xFFFFFF00
        reader = QDataStreamReader(blob)  # default 1M limit
        with pytest.raises(QDataStreamError, match="exceeds max_container_items"):
            from quasseltui.qt.variant import read_qvariantlist as _read

            _read(reader)

    def test_count_at_limit_succeeds(self) -> None:
        # Build a list of exactly the limit, then read it back.
        items = [1, 2, 3]
        writer = QDataStreamWriter()
        from quasseltui.qt.variant import write_qvariantlist as _write

        _write(writer, items)
        reader = QDataStreamReader(writer.to_bytes(), max_container_items=3)
        from quasseltui.qt.variant import read_qvariantlist as _read

        assert _read(reader) == items


class TestUserTypeEnvelope:
    """The QVariant<UserType> envelope is its own little wire format. These
    tests pin the bytes against hand-built blobs so any silent change
    (forgetting the trailing-null normalization, swapping QString for
    QByteArray on the name, ...) blows up loudly.
    """

    def setup_method(self) -> None:
        from quasseltui.qt.usertypes import register_user_type

        # Toy "Pair" type: payload is two uint32s. We register it for the
        # duration of these tests and tear down afterwards so no test
        # pollution leaks into the rest of the suite.
        def _read(reader: QDataStreamReader) -> tuple[int, int]:
            return (reader.read_uint32(), reader.read_uint32())

        def _write(writer: QDataStreamWriter, value: object) -> None:
            assert isinstance(value, tuple)
            writer.write_uint32(int(value[0]))
            writer.write_uint32(int(value[1]))

        register_user_type(b"PairTest", _read, _write)

    def teardown_method(self) -> None:
        from quasseltui.qt.usertypes import _USER_TYPE_READERS, _USER_TYPE_WRITERS

        _USER_TYPE_READERS.pop(b"PairTest", None)
        _USER_TYPE_WRITERS.pop(b"PairTest", None)

    def test_user_type_round_trip_with_explicit_name(self) -> None:
        writer = QDataStreamWriter()
        write_variant(writer, (3, 7), user_type_name=b"PairTest")
        reader = QDataStreamReader(writer.to_bytes())
        assert read_variant(reader) == (3, 7)
        assert reader.at_end()

    def test_user_type_envelope_byte_layout(self) -> None:
        # type=127, is_null=0, name=QByteArray("PairTest"), payload=(3, 7)
        writer = QDataStreamWriter()
        write_variant(writer, (3, 7), user_type_name=b"PairTest")
        blob = writer.to_bytes()
        # 4-byte type id
        assert blob[:4] == b"\x00\x00\x00\x7f"  # 127
        # 1-byte is_null = 0
        assert blob[4] == 0
        # 4-byte QByteArray length, then "PairTest" (no trailing null on write)
        assert blob[5:9] == b"\x00\x00\x00\x08"
        assert blob[9:17] == b"PairTest"
        # 8 bytes of payload
        assert blob[17:25] == b"\x00\x00\x00\x03\x00\x00\x00\x07"

    def test_user_type_name_with_trailing_nulls_is_normalized_on_read(self) -> None:
        # Hand-build the envelope with the name as "PairTest\0" — this is what
        # an older Quassel core would emit. Our decoder should strip the NUL
        # and look up the same handler.
        writer = QDataStreamWriter()
        writer.write_uint32(127)  # UserType
        writer.write_uint8(0)  # not null
        writer.write_qbytearray(b"PairTest\x00")
        writer.write_uint32(11)
        writer.write_uint32(13)
        reader = QDataStreamReader(writer.to_bytes())
        assert read_variant(reader) == (11, 13)
        assert reader.at_end()

    def test_unregistered_user_type_raises(self) -> None:
        writer = QDataStreamWriter()
        writer.write_uint32(127)
        writer.write_uint8(0)
        writer.write_qbytearray(b"Mystery")
        # Garbage payload — we shouldn't even get this far before the lookup
        # fails.
        writer.write_uint32(0)
        reader = QDataStreamReader(writer.to_bytes())
        with pytest.raises(QDataStreamError, match=r"unsupported QVariant<UserType> name"):
            read_variant(reader)

    def test_user_type_with_null_flag_consumes_payload(self) -> None:
        # is_null=1 must NOT skip the payload — the payload is still on the
        # wire because Qt's QVariant::save always serializes it. The decoded
        # value collapses to None for the caller, but the stream cursor must
        # still advance past those bytes.
        writer = QDataStreamWriter()
        writer.write_uint32(127)
        writer.write_uint8(1)  # is_null
        writer.write_qbytearray(b"PairTest")
        writer.write_uint32(0)  # payload byte 1
        writer.write_uint32(0)  # payload byte 2
        # And a follow-on Int variant we'd misread if the cursor was wrong.
        write_variant(writer, 42, type_id=QMetaType.Int)

        reader = QDataStreamReader(writer.to_bytes())
        first = read_variant(reader)
        second = read_variant(reader)
        assert first is None
        assert second == 42
        assert reader.at_end()

    def test_write_with_both_type_id_and_user_type_name_rejected(self) -> None:
        writer = QDataStreamWriter()
        with pytest.raises(QDataStreamError, match="not both"):
            write_variant(
                writer,
                (1, 2),
                type_id=QMetaType.QVariantList,
                user_type_name=b"PairTest",
            )


class TestShortAndQDateTime:
    """Phase 4 added `Short = 130` and `QDateTime = 16` to the dispatch.

    The Short ID is the one thing that would silently break SignalProxy
    decoding if we got the number wrong — Quassel uses its own 130, NOT
    Qt's QMetaType::Short = 33. Pin the ID in a byte-layout test so any
    regression surfaces immediately.
    """

    def test_short_round_trip(self) -> None:
        writer = QDataStreamWriter()
        write_variant(writer, 42, type_id=QMetaType.Short)
        reader = QDataStreamReader(writer.to_bytes())
        assert read_variant(reader) == 42
        assert reader.at_end()

    def test_short_wire_type_id_is_130(self) -> None:
        """The type-id header is Quassel's `Types::VariantType::Short = 130`."""
        writer = QDataStreamWriter()
        write_variant(writer, 1, type_id=QMetaType.Short)
        blob = writer.to_bytes()
        # 4-byte type id (big-endian 130) + 1-byte is_null + 2-byte value
        assert blob[:4] == b"\x00\x00\x00\x82"
        assert blob[4] == 0
        assert blob[5:7] == b"\x00\x01"

    def test_short_negative_roundtrip(self) -> None:
        writer = QDataStreamWriter()
        write_variant(writer, -1, type_id=QMetaType.Short)
        reader = QDataStreamReader(writer.to_bytes())
        assert read_variant(reader) == -1

    def test_qdatetime_round_trip_utc(self) -> None:
        import datetime as dt

        value = dt.datetime(2026, 4, 14, 12, 34, 56, 789_000, tzinfo=dt.UTC)
        writer = QDataStreamWriter()
        write_variant(writer, value, type_id=QMetaType.QDateTime)
        reader = QDataStreamReader(writer.to_bytes())
        result = read_variant(reader)
        assert result == value

    def test_qdatetime_is_inferred_from_python_datetime(self) -> None:
        import datetime as dt

        value = dt.datetime(2026, 4, 14, 12, 0, 0, tzinfo=dt.UTC)
        writer = QDataStreamWriter()
        write_variant(writer, value)  # no explicit type_id
        blob = writer.to_bytes()
        # The type id should be 16 (QDateTime), not 0 (Invalid) or 1024.
        assert blob[:4] == b"\x00\x00\x00\x10"
        reader = QDataStreamReader(blob)
        assert read_variant(reader) == value

    def test_qdatetime_write_rejects_non_datetime(self) -> None:
        writer = QDataStreamWriter()
        with pytest.raises(TypeError, match="cannot serialize"):
            write_variant(writer, "not a datetime", type_id=QMetaType.QDateTime)
