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
