"""Unit tests for Quassel custom user types.

The user-type wire formats are pinned against hand-built byte blobs derived
from the Quassel C++ source — `src/common/bufferinfo.cpp` and `types.h`.
Every BufferInfo we send or receive passes through this codec so silent
desyncs here would manifest as confusing parser failures elsewhere.
"""

from __future__ import annotations

import struct

import pytest

from quasseltui.protocol.usertypes import (
    USER_TYPE_BUFFER_ID,
    USER_TYPE_BUFFER_INFO,
    USER_TYPE_IDENTITY,
    USER_TYPE_IDENTITY_ID,
    USER_TYPE_MSG_ID,
    USER_TYPE_NETWORK_ID,
    BufferId,
    BufferInfo,
    BufferType,
    IdentityId,
    MsgId,
    NetworkId,
)
from quasseltui.qt.datastream import QDataStreamError, QDataStreamReader, QDataStreamWriter
from quasseltui.qt.variant import read_variant, write_variant


class TestSignedIdentifiers:
    """BufferId / NetworkId / IdentityId / MsgId all wrap a single int and
    are written as the matching qint primitive on the wire — no length
    prefix, no extra envelope. The QVariant<UserType> dispatch in
    `read_variant` is what gives them their identity.
    """

    @pytest.mark.parametrize(
        ("dataclass", "name", "value"),
        [
            (BufferId, USER_TYPE_BUFFER_ID, 1),
            (BufferId, USER_TYPE_BUFFER_ID, 2_000_000_000),
            (BufferId, USER_TYPE_BUFFER_ID, -1),
            (NetworkId, USER_TYPE_NETWORK_ID, 7),
            (IdentityId, USER_TYPE_IDENTITY_ID, 99),
        ],
    )
    def test_int32_id_round_trip(self, dataclass: type, name: bytes, value: int) -> None:
        writer = QDataStreamWriter()
        write_variant(writer, dataclass(value), user_type_name=name)
        reader = QDataStreamReader(writer.to_bytes())
        result = read_variant(reader)
        assert isinstance(result, dataclass)
        assert int(result) == value
        assert reader.at_end()

    def test_msg_id_uses_qint64(self) -> None:
        big = 10**12  # > qint32 range
        writer = QDataStreamWriter()
        write_variant(writer, MsgId(big), user_type_name=USER_TYPE_MSG_ID)
        # Envelope: 4 bytes type, 1 byte is_null, 4+5 bytes name(=MsgId)
        # = "MsgId" length 5, then 8 bytes payload (qint64).
        blob = writer.to_bytes()
        # The last 8 bytes should be the big-endian qint64 of `big`.
        assert blob[-8:] == struct.pack(">q", big)
        reader = QDataStreamReader(blob)
        result = read_variant(reader)
        assert isinstance(result, MsgId)
        assert int(result) == big


class TestBufferInfo:
    """BufferInfo's `operator<<` is the largest user type we ship with phase
    3. It writes 5 fields in a specific order — anything else will silently
    decode to garbage. The byte-layout test below pins it.
    """

    def test_round_trip_all_buffer_kinds(self) -> None:
        original = BufferInfo(
            buffer_id=BufferId(42),
            network_id=NetworkId(2),
            type=BufferType.Channel,
            group_id=0,
            name="#python",
        )
        writer = QDataStreamWriter()
        write_variant(writer, original, user_type_name=USER_TYPE_BUFFER_INFO)
        reader = QDataStreamReader(writer.to_bytes())
        result = read_variant(reader)
        assert result == original
        assert reader.at_end()

    def test_buffer_info_payload_byte_layout(self) -> None:
        """Pin the exact byte order for BufferInfo's `operator<<`:

        qint32 bufferId
        qint32 networkId
        qint16 type
        quint32 groupId
        QByteArray name (UTF-8 of bufferName)
        """
        buf = BufferInfo(
            buffer_id=BufferId(0x11223344),
            network_id=NetworkId(0x55667788),
            type=BufferType.Query,  # = 0x04
            group_id=0xAABBCCDD,
            name="x",
        )
        writer = QDataStreamWriter()
        # Skip the envelope and hand-encode just the payload, then compare.
        from quasseltui.protocol.usertypes import _write_buffer_info

        _write_buffer_info(writer, buf)
        blob = writer.to_bytes()
        expected = (
            b"\x11\x22\x33\x44"  # bufferId qint32 (BE)
            b"\x55\x66\x77\x88"  # networkId qint32
            b"\x00\x04"  # type qint16 (Query)
            b"\xaa\xbb\xcc\xdd"  # groupId quint32
            b"\x00\x00\x00\x01"  # name length (1)
            b"x"  # UTF-8 of name
        )
        assert blob == expected

    def test_buffer_info_with_unicode_name(self) -> None:
        buf = BufferInfo(
            buffer_id=BufferId(1),
            network_id=NetworkId(1),
            type=BufferType.Channel,
            group_id=0,
            name="#日本語",
        )
        writer = QDataStreamWriter()
        write_variant(writer, buf, user_type_name=USER_TYPE_BUFFER_INFO)
        reader = QDataStreamReader(writer.to_bytes())
        result = read_variant(reader)
        assert isinstance(result, BufferInfo)
        assert result.name == "#日本語"

    def test_buffer_info_unknown_type_coerced_to_invalid(self) -> None:
        # A future Quassel could add a new buffer kind. The decoder should
        # not crash on it — coerce to Invalid and keep going so the rest
        # of the surrounding stream still parses.
        writer = QDataStreamWriter()
        writer.write_int32(1)  # bufferId
        writer.write_int32(1)  # networkId
        writer.write_int16(0x40)  # type — not a known BufferType value
        writer.write_uint32(0)  # groupId
        writer.write_qbytearray(b"#future")
        from quasseltui.protocol.usertypes import _read_buffer_info

        result = _read_buffer_info(QDataStreamReader(writer.to_bytes()))
        assert result.type is BufferType.Invalid
        assert result.name == "#future"

    def test_buffer_info_writer_rejects_wrong_type(self) -> None:
        writer = QDataStreamWriter()
        from quasseltui.protocol.usertypes import _write_buffer_info

        with pytest.raises(QDataStreamError, match="expected BufferInfo"):
            _write_buffer_info(writer, "not a bufferinfo")


class TestIdentityPayload:
    """Identity is a SyncObject in Quassel, but on the wire its user-type
    payload is just a serialized `QVariantMap`. We expose it as a raw dict
    here — phase 5 will model it properly.
    """

    def test_identity_round_trip_as_dict(self) -> None:
        original_map = {
            "identityName": "my-identity",
            "realName": "Sean R.",
            "ident": "sean",
            "nicks": ["sean", "sean_", "sean__"],
            "autoAwayEnabled": False,
        }
        writer = QDataStreamWriter()
        write_variant(writer, original_map, user_type_name=USER_TYPE_IDENTITY)
        reader = QDataStreamReader(writer.to_bytes())
        decoded = read_variant(reader)
        assert decoded == original_map

    def test_identity_writer_rejects_non_dict(self) -> None:
        writer = QDataStreamWriter()
        from quasseltui.protocol.usertypes import _write_identity

        with pytest.raises(QDataStreamError, match="expected dict"):
            _write_identity(writer, [1, 2, 3])


class TestUserTypeIntegrationWithVariantList:
    """A QVariantList full of QVariant<UserType> values is the actual shape
    the SessionInit message uses for `NetworkIds` and `BufferInfos`. This
    test exercises the round trip end-to-end through the dispatch table to
    make sure the recursive container path doesn't lose the user-type
    envelopes."""

    def test_list_of_network_ids(self) -> None:
        ids = [NetworkId(1), NetworkId(2), NetworkId(99)]
        writer = QDataStreamWriter()

        # Manually wrap each NetworkId in a QVariant<UserType> envelope so
        # that we mimic exactly what the core does.
        writer.write_uint32(len(ids))
        for nid in ids:
            write_variant(writer, nid, user_type_name=USER_TYPE_NETWORK_ID)
        reader = QDataStreamReader(writer.to_bytes())
        from quasseltui.qt.variant import read_qvariantlist

        decoded = read_qvariantlist(reader)
        assert decoded == ids
