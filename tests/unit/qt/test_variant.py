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
