"""Unit tests for handshake message serialization.

The handshake wire format is the most likely place to silently produce
something a real Quassel core will reject, so these tests pin the exact
behavior:

- Keys are wrapped as `QVariant<QByteArray>` (UTF-8) — NOT `QVariant<QString>`
- Values fall through to `write_variant` type inference except `Features`
  which we force to `UInt`, and `FeatureList` which we force to `QStringList`
- Sorted-key order on emission (Qt's QMap iteration order)
- Decode is order-insensitive — we round-trip arbitrary key orders back
  through the dict
- Even-but-empty payloads decode to `{}`; odd-length payloads fail loudly
"""

from __future__ import annotations

import asyncio

import pytest

from quasseltui.protocol.errors import HandshakeError
from quasseltui.protocol.framing import encode_frame
from quasseltui.protocol.handshake import (
    decode_handshake_payload,
    encode_client_init,
    encode_handshake_payload,
    recv_handshake_message,
)
from quasseltui.protocol.messages import (
    CLIENT_INIT,
    ClientInit,
    ClientInitAck,
    ClientInitReject,
)
from quasseltui.qt.datastream import QDataStreamReader
from quasseltui.qt.types import QMetaType
from quasseltui.qt.variant import read_qvariantlist


class TestEncodeHandshakePayload:
    def test_keys_are_qbytearray_in_sorted_order(self) -> None:
        payload = encode_handshake_payload({"zeta": 1, "alpha": "x"})
        reader = QDataStreamReader(payload)
        items = read_qvariantlist(reader)
        # 2 keys, each emitted as (key + value) -> 4 items
        assert len(items) == 4
        # Even indices are decoded as `bytes` because the on-wire type
        # was QByteArray (not QString).
        assert items[0] == b"alpha"
        assert items[1] == "x"
        assert items[2] == b"zeta"
        assert items[3] == 1

    def test_features_field_serialized_as_uint(self) -> None:
        # Features bit 31 set — would mis-encode as a negative quint32 if
        # we accidentally fell back to Int (qint32).
        payload = encode_handshake_payload({"Features": 0x80000001})
        # Find the Features value by walking the payload. Skip the
        # QVariantList count (4 bytes) and the first key entry.
        reader = QDataStreamReader(payload)
        count = reader.read_uint32()
        assert count == 2
        # First item: QVariant<QByteArray> for the key
        key_type = reader.read_uint32()
        assert key_type == QMetaType.QByteArray
        reader.read_uint8()  # is_null
        key_len = reader.read_uint32()
        assert reader.read_bytes(key_len) == b"Features"
        # Second item: QVariant<UInt> for the value (NOT Int)
        value_type = reader.read_uint32()
        assert value_type == QMetaType.UInt
        reader.read_uint8()  # is_null
        assert reader.read_uint32() == 0x80000001

    def test_feature_list_serialized_as_qstringlist(self) -> None:
        payload = encode_handshake_payload(
            {"FeatureList": ["SynchronizedMarkerLine", "SaslAuthentication"]}
        )
        reader = QDataStreamReader(payload)
        assert reader.read_uint32() == 2  # count
        # Skip the key
        reader.read_uint32()  # type
        reader.read_uint8()  # is_null
        key_len = reader.read_uint32()
        reader.read_bytes(key_len)
        # Value should be a QStringList variant, not a QVariantList
        value_type = reader.read_uint32()
        assert value_type == QMetaType.QStringList


class TestRoundTripHandshakePayload:
    def test_client_init_round_trips(self) -> None:
        original = {
            "MsgType": CLIENT_INIT,
            "ClientVersion": "quasseltui v0.0.0",
            "ClientDate": "2026-04-14",
            "Features": 0,
            "FeatureList": [],
        }
        encoded = encode_handshake_payload(original)
        decoded = decode_handshake_payload(encoded)
        assert decoded == original

    def test_decode_is_order_insensitive(self) -> None:
        # Build a payload whose keys are in reverse-sorted order on the wire.
        # The decoder should still produce an equal dict because we
        # rebuild from the list.
        payload_a = encode_handshake_payload({"a": 1, "b": 2})
        payload_b = encode_handshake_payload({"b": 2, "a": 1})
        # `encode_handshake_payload` always sorts, so both produce the same
        # bytes — that's the proof of canonicalization.
        assert payload_a == payload_b
        assert decode_handshake_payload(payload_a) == {"a": 1, "b": 2}

    def test_empty_payload_decodes_to_empty_dict(self) -> None:
        encoded = encode_handshake_payload({})
        assert decode_handshake_payload(encoded) == {}


class TestDecodeHandshakeErrors:
    def test_odd_length_payload_rejected(self) -> None:
        # Build a 1-item list manually and feed it in.
        from quasseltui.qt.datastream import QDataStreamWriter
        from quasseltui.qt.variant import write_variant

        writer = QDataStreamWriter()
        writer.write_uint32(1)
        write_variant(writer, b"orphan", type_id=QMetaType.QByteArray)
        with pytest.raises(HandshakeError, match="odd item count"):
            decode_handshake_payload(writer.to_bytes())

    def test_non_string_key_rejected(self) -> None:
        from quasseltui.qt.datastream import QDataStreamWriter
        from quasseltui.qt.variant import write_variant

        writer = QDataStreamWriter()
        writer.write_uint32(2)
        write_variant(writer, 42, type_id=QMetaType.Int)  # int as a key
        write_variant(writer, "value", type_id=QMetaType.QString)
        with pytest.raises(HandshakeError, match="unexpected type"):
            decode_handshake_payload(writer.to_bytes())

    def test_trailing_bytes_rejected(self) -> None:
        encoded = encode_handshake_payload({"k": "v"}) + b"junk"
        with pytest.raises(HandshakeError, match="trailing"):
            decode_handshake_payload(encoded)


class TestEncodeClientInit:
    def test_produces_full_field_set(self) -> None:
        msg = ClientInit(
            client_version="quasseltui v0.0.0",
            build_date="2026-04-14",
        )
        decoded = decode_handshake_payload(encode_client_init(msg))
        assert decoded == {
            "MsgType": CLIENT_INIT,
            "ClientVersion": "quasseltui v0.0.0",
            "ClientDate": "2026-04-14",
            "Features": 0,
            "FeatureList": [],
        }

    def test_with_features_and_list(self) -> None:
        msg = ClientInit(
            client_version="quasseltui v0.0.0",
            build_date="2026-04-14",
            features=0xC03F,
            feature_list=("SynchronizedMarkerLine", "SaslAuthentication"),
        )
        decoded = decode_handshake_payload(encode_client_init(msg))
        assert decoded["Features"] == 0xC03F
        assert decoded["FeatureList"] == [
            "SynchronizedMarkerLine",
            "SaslAuthentication",
        ]


class TestParseClientInitAck:
    def test_minimal_ack(self) -> None:
        ack_map = {
            "MsgType": "ClientInitAck",
            "CoreFeatures": 0xC03F,
            "FeatureList": ["SynchronizedMarkerLine"],
            "Configured": True,
            "ProtocolVersion": 10,
        }
        encoded = encode_handshake_payload(ack_map)
        decoded = decode_handshake_payload(encoded)
        ack = ClientInitAck.from_map(decoded)
        assert ack.core_features == 0xC03F
        assert ack.feature_list == ("SynchronizedMarkerLine",)
        assert ack.configured is True
        assert ack.protocol_version == 10
        assert ack.storage_backends == ()
        assert ack.authenticators == ()

    def test_unconfigured_core_with_backends(self) -> None:
        ack_map = {
            "MsgType": "ClientInitAck",
            "CoreFeatures": 0,
            "FeatureList": [],
            "Configured": False,
            "StorageBackends": [
                {
                    "DisplayName": "SQLite",
                    "Description": "Default file-backed storage",
                    "SetupKeys": ["Database"],
                    "SetupDefaults": {"Database": "quassel-storage.sqlite"},
                },
            ],
            "Authenticators": [
                {"DisplayName": "Database", "Description": "Use the storage DB"},
            ],
        }
        encoded = encode_handshake_payload(ack_map)
        decoded = decode_handshake_payload(encoded)
        ack = ClientInitAck.from_map(decoded)
        assert ack.configured is False
        assert len(ack.storage_backends) == 1
        assert ack.storage_backends[0].display_name == "SQLite"
        assert ack.storage_backends[0].setup_keys == ("Database",)
        assert ack.storage_backends[0].setup_defaults == {
            "Database": "quassel-storage.sqlite",
        }
        assert len(ack.authenticators) == 1
        assert ack.authenticators[0].display_name == "Database"

    def test_reject_dispatched_correctly(self) -> None:
        from quasseltui.protocol.messages import parse_handshake_message

        encoded = encode_handshake_payload({"MsgType": "ClientInitReject", "Error": "core too old"})
        decoded = decode_handshake_payload(encoded)
        result = parse_handshake_message(decoded)
        assert isinstance(result, ClientInitReject)
        assert result.error_string == "core too old"

    def test_unknown_msgtype_raises(self) -> None:
        from quasseltui.protocol.messages import parse_handshake_message

        with pytest.raises(HandshakeError, match="unknown handshake MsgType"):
            parse_handshake_message({"MsgType": "Mystery"})

    def test_missing_msgtype_raises(self) -> None:
        from quasseltui.protocol.messages import parse_handshake_message

        with pytest.raises(HandshakeError, match="no MsgType"):
            parse_handshake_message({})


class TestRecvHandshakeMessage:
    @pytest.mark.asyncio
    async def test_reads_framed_ack(self) -> None:
        ack_map = {
            "MsgType": "ClientInitAck",
            "CoreFeatures": 0,
            "FeatureList": [],
            "Configured": True,
        }
        payload = encode_handshake_payload(ack_map)
        reader = asyncio.StreamReader()
        reader.feed_data(encode_frame(payload))
        reader.feed_eof()

        result = await recv_handshake_message(reader)
        assert isinstance(result, ClientInitAck)
        assert result.configured is True

    @pytest.mark.asyncio
    async def test_reads_framed_reject(self) -> None:
        payload = encode_handshake_payload({"MsgType": "ClientInitReject", "Error": "no good"})
        reader = asyncio.StreamReader()
        reader.feed_data(encode_frame(payload))
        reader.feed_eof()

        result = await recv_handshake_message(reader)
        assert isinstance(result, ClientInitReject)
        assert result.error_string == "no good"
