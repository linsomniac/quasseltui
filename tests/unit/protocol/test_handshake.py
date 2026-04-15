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

from quasseltui.protocol.errors import AuthError, HandshakeError
from quasseltui.protocol.framing import encode_frame
from quasseltui.protocol.handshake import (
    decode_handshake_payload,
    encode_client_init,
    encode_client_login,
    encode_handshake_payload,
    recv_handshake_message,
)
from quasseltui.protocol.messages import (
    CLIENT_INIT,
    CLIENT_LOGIN,
    ClientInit,
    ClientInitAck,
    ClientInitReject,
    ClientLogin,
    ClientLoginAck,
    CoreSetupReject,
    SessionInit,
    parse_handshake_message,
)
from quasseltui.protocol.usertypes import (
    USER_TYPE_BUFFER_INFO,
    USER_TYPE_NETWORK_ID,
    BufferId,
    BufferInfo,
    BufferType,
    NetworkId,
)
from quasseltui.qt.datastream import QDataStreamReader, QDataStreamWriter
from quasseltui.qt.types import QMetaType
from quasseltui.qt.variant import read_qvariantlist, write_variant


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


class TestStrictClientInitAckParsing:
    """Codex review caught that `int(data["ProtocolVersion"])` and similar
    casts can leak `TypeError`/`ValueError` past the `QuasselError` handler.
    These tests pin that all schema/type failures arrive as `HandshakeError`.
    """

    def _ack(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "MsgType": "ClientInitAck",
            "CoreFeatures": 0,
            "FeatureList": [],
            "Configured": True,
        }
        base.update(overrides)
        return base

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(HandshakeError, match="missing required field 'CoreFeatures'"):
            ClientInitAck.from_map({"MsgType": "ClientInitAck", "Configured": True})

    def test_wrong_type_for_int_field_rejected(self) -> None:
        with pytest.raises(HandshakeError, match=r"CoreFeatures.*expected int"):
            ClientInitAck.from_map(self._ack(CoreFeatures="not-an-int"))

    def test_bool_in_int_field_rejected(self) -> None:
        # `bool` is an `int` subclass, so a naive isinstance check would
        # let `True` through as a valid integer. Defensive type check
        # rejects it explicitly.
        with pytest.raises(HandshakeError, match=r"CoreFeatures.*expected int"):
            ClientInitAck.from_map(self._ack(CoreFeatures=True))

    def test_wrong_type_for_bool_field_rejected(self) -> None:
        with pytest.raises(HandshakeError, match=r"Configured.*expected bool"):
            ClientInitAck.from_map(self._ack(Configured=1))

    def test_wrong_type_for_string_in_list_rejected(self) -> None:
        with pytest.raises(HandshakeError, match=r"FeatureList'\[1\].*expected str"):
            ClientInitAck.from_map(self._ack(FeatureList=["ok", 42]))

    def test_wrong_type_for_optional_int_field_rejected(self) -> None:
        with pytest.raises(HandshakeError, match=r"ProtocolVersion.*expected int"):
            ClientInitAck.from_map(self._ack(ProtocolVersion="ten"))

    def test_storage_backends_must_be_list(self) -> None:
        with pytest.raises(HandshakeError, match=r"StorageBackends.*expected list"):
            ClientInitAck.from_map(self._ack(StorageBackends="oops"))

    def test_optional_protocol_version_absence_ok(self) -> None:
        ack = ClientInitAck.from_map(self._ack())
        assert ack.protocol_version is None

    def test_minimal_valid_ack_still_parses(self) -> None:
        # Sanity: the strict validator did not break the canonical case.
        ack = ClientInitAck.from_map(self._ack(CoreFeatures=0xC03F, ProtocolVersion=10))
        assert ack.core_features == 0xC03F
        assert ack.protocol_version == 10
        assert ack.configured is True


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


class TestEncodeClientLogin:
    def test_produces_user_password_pair(self) -> None:
        msg = ClientLogin(user="sean", password="hunter2")
        decoded = decode_handshake_payload(encode_client_login(msg))
        assert decoded == {
            "MsgType": CLIENT_LOGIN,
            "User": "sean",
            "Password": "hunter2",
        }

    def test_password_with_unicode_round_trips(self) -> None:
        msg = ClientLogin(user="user", password="🔒passwørd")
        decoded = decode_handshake_payload(encode_client_login(msg))
        assert decoded["Password"] == "🔒passwørd"


class TestParseClientLogin:
    def test_login_ack_parses(self) -> None:
        result = parse_handshake_message({"MsgType": "ClientLoginAck"})
        assert isinstance(result, ClientLoginAck)

    def test_login_reject_raises_auth_error(self) -> None:
        # Credentials failures bounce out as an exception so the connection
        # state machine can't accidentally fall through to "all good" — the
        # type system guarantees you handled it.
        with pytest.raises(AuthError, match="bad password"):
            parse_handshake_message({"MsgType": "ClientLoginReject", "Error": "bad password"})

    def test_login_reject_with_no_error_string_still_raises(self) -> None:
        with pytest.raises(AuthError, match="core rejected credentials"):
            parse_handshake_message({"MsgType": "ClientLoginReject"})

    def test_core_setup_reject_returned_as_value(self) -> None:
        # CoreSetupReject is informational (we don't run setup) so it stays
        # a regular return value — the CLI surfaces it as a friendly error.
        result = parse_handshake_message({"MsgType": "CoreSetupReject", "Error": "missing field"})
        assert isinstance(result, CoreSetupReject)
        assert result.error_string == "missing field"


class TestParseSessionInit:
    """SessionInit is the meatiest handshake message — its `SessionState`
    nested map carries lists of user-type values. These tests verify the
    structural validation and the typed unpacking, not the Quassel byte
    format (that lives in `test_usertypes.py`).
    """

    def _build_session_state(
        self,
        *,
        identities: list[dict[str, object]] | None = None,
        network_ids: list[NetworkId] | None = None,
        buffer_infos: list[BufferInfo] | None = None,
    ) -> dict[str, object]:
        return {
            "MsgType": "SessionInit",
            "SessionState": {
                "Identities": identities or [],
                "NetworkIds": network_ids or [],
                "BufferInfos": buffer_infos or [],
            },
        }

    def test_empty_session_state_parses(self) -> None:
        result = parse_handshake_message(self._build_session_state())
        assert isinstance(result, SessionInit)
        assert result.identities == ()
        assert result.network_ids == ()
        assert result.buffer_infos == ()

    def test_full_session_state(self) -> None:
        result = parse_handshake_message(
            self._build_session_state(
                identities=[{"identityName": "default", "identityId": 1}],
                network_ids=[NetworkId(1), NetworkId(2)],
                buffer_infos=[
                    BufferInfo(
                        buffer_id=BufferId(10),
                        network_id=NetworkId(1),
                        type=BufferType.Channel,
                        group_id=0,
                        name="#python",
                    ),
                    BufferInfo(
                        buffer_id=BufferId(11),
                        network_id=NetworkId(1),
                        type=BufferType.Status,
                        group_id=0,
                        name="",
                    ),
                ],
            )
        )
        assert isinstance(result, SessionInit)
        assert len(result.identities) == 1
        assert result.identities[0]["identityName"] == "default"
        assert result.network_ids == (NetworkId(1), NetworkId(2))
        assert len(result.buffer_infos) == 2
        assert result.buffer_infos[0].name == "#python"

    def test_missing_session_state_rejected(self) -> None:
        with pytest.raises(HandshakeError, match="missing required field 'SessionState'"):
            parse_handshake_message({"MsgType": "SessionInit"})

    def test_session_state_wrong_type_rejected(self) -> None:
        with pytest.raises(HandshakeError, match=r"SessionState.*expected dict"):
            parse_handshake_message({"MsgType": "SessionInit", "SessionState": "oops"})

    def test_identities_must_be_list_of_dicts(self) -> None:
        with pytest.raises(HandshakeError, match=r"Identities'\[0\].*expected dict"):
            parse_handshake_message(
                {
                    "MsgType": "SessionInit",
                    "SessionState": {
                        "Identities": ["not a dict"],
                        "NetworkIds": [],
                        "BufferInfos": [],
                    },
                }
            )

    def test_network_ids_must_be_network_id_instances(self) -> None:
        # If a future core sends raw ints in NetworkIds (skipping the user
        # type wrapper) we should refuse — better to crash visibly than
        # decode something we'll later mis-key on.
        with pytest.raises(HandshakeError, match=r"NetworkIds'\[0\].*expected NetworkId"):
            parse_handshake_message(
                {
                    "MsgType": "SessionInit",
                    "SessionState": {
                        "Identities": [],
                        "NetworkIds": [42],
                        "BufferInfos": [],
                    },
                }
            )

    def test_buffer_infos_must_be_buffer_info_instances(self) -> None:
        with pytest.raises(HandshakeError, match=r"BufferInfos'\[0\].*expected BufferInfo"):
            parse_handshake_message(
                {
                    "MsgType": "SessionInit",
                    "SessionState": {
                        "Identities": [],
                        "NetworkIds": [],
                        "BufferInfos": [{"not": "a bufferinfo"}],
                    },
                }
            )

    def test_session_init_round_trips_through_full_codec(self) -> None:
        """Encode a SessionInit envelope by hand and decode it back through
        `decode_handshake_payload` + `parse_handshake_message`.

        We can't call `encode_handshake_payload({"SessionState": {...}})`
        with our dataclasses inside, because `write_variant`'s type
        inference doesn't know about `BufferInfo` etc. So we hand-build the
        outer flattened-map payload to mirror exactly what a real core
        would emit — and the decoder/parser still has to put it back
        together correctly.
        """
        bi = BufferInfo(
            buffer_id=BufferId(1),
            network_id=NetworkId(1),
            type=BufferType.Channel,
            group_id=0,
            name="#test",
        )

        # The flattened handshake payload is a QVariantList of
        # [key0, value0, key1, value1, ...] where keys are
        # QVariant<QByteArray> and values keep their typed envelopes.
        writer = QDataStreamWriter()
        writer.write_uint32(2 * 2)  # 2 fields, 2 entries each (key + value)

        write_variant(writer, b"MsgType", type_id=QMetaType.QByteArray)
        write_variant(writer, "SessionInit", type_id=QMetaType.QString)

        write_variant(writer, b"SessionState", type_id=QMetaType.QByteArray)

        # Nested QVariant<QVariantMap> for SessionState. Hand-encode the
        # envelope (type=8, is_null=0) followed by the QVariantMap body.
        writer.write_uint32(QMetaType.QVariantMap)
        writer.write_uint8(0)
        writer.write_uint32(3)  # 3 keys in the inner map

        # Each entry: QString key, then a typed QVariant value.
        writer.write_qstring("BufferInfos")
        writer.write_uint32(QMetaType.QVariantList)
        writer.write_uint8(0)
        writer.write_uint32(1)
        write_variant(writer, bi, user_type_name=USER_TYPE_BUFFER_INFO)

        writer.write_qstring("Identities")
        writer.write_uint32(QMetaType.QVariantList)
        writer.write_uint8(0)
        writer.write_uint32(0)  # empty list

        writer.write_qstring("NetworkIds")
        writer.write_uint32(QMetaType.QVariantList)
        writer.write_uint8(0)
        writer.write_uint32(1)
        write_variant(writer, NetworkId(1), user_type_name=USER_TYPE_NETWORK_ID)

        decoded = decode_handshake_payload(writer.to_bytes())
        result = parse_handshake_message(decoded)
        assert isinstance(result, SessionInit)
        assert result.network_ids == (NetworkId(1),)
        assert result.buffer_infos == (bi,)
        assert result.identities == ()
