"""Unit tests for the SignalProxy codec.

These pin the wire format of the six CONNECTED-state message kinds. The
discriminator is `qint16`-in-QVariant<Short> (Quassel type id 130), not an
`int` (type id 2) — getting that wrong would make the first byte of every
frame unparseable by a real core. The round-trip tests catch it in isolation;
the byte-layout tests catch it with authoritative reference bytes.
"""

from __future__ import annotations

import datetime as dt

import pytest

from quasseltui.protocol.signalproxy import (
    REQUEST_HEARTBEAT,
    REQUEST_HEARTBEAT_REPLY,
    REQUEST_INIT_DATA,
    REQUEST_INIT_REQUEST,
    REQUEST_RPC_CALL,
    REQUEST_SYNC,
    HeartBeat,
    HeartBeatReply,
    InitData,
    InitRequest,
    RpcCall,
    SignalProxyError,
    SyncMessage,
    decode_signalproxy_payload,
    encode_signalproxy_payload,
)
from quasseltui.qt.datastream import QDataStreamReader
from quasseltui.qt.variant import read_qvariantlist


def _decode_list(payload: bytes) -> list[object]:
    reader = QDataStreamReader(payload)
    items = read_qvariantlist(reader)
    assert reader.at_end()
    return list(items)


class TestSyncRoundtrip:
    def test_basic_sync_roundtrip(self) -> None:
        original = SyncMessage(
            class_name=b"Network",
            object_name="1",
            slot_name=b"setNetworkName",
            params=["freenode"],
        )
        blob = encode_signalproxy_payload(original)
        decoded = decode_signalproxy_payload(blob)
        assert isinstance(decoded, SyncMessage)
        assert decoded == original

    def test_sync_with_no_params(self) -> None:
        original = SyncMessage(
            class_name=b"IrcChannel",
            object_name="1/#python",
            slot_name=b"joinIrcUser",
            params=[],
        )
        decoded = decode_signalproxy_payload(encode_signalproxy_payload(original))
        assert decoded == original

    def test_sync_with_multiple_params(self) -> None:
        original = SyncMessage(
            class_name=b"BufferSyncer",
            object_name="",
            slot_name=b"markBufferAsRead",
            params=[42, "extra", True],
        )
        decoded = decode_signalproxy_payload(encode_signalproxy_payload(original))
        assert decoded == original

    def test_sync_discriminator_is_short_not_int(self) -> None:
        """The first element MUST be a QVariant<Short>, not QVariant<Int>.

        Quassel's handlePackedFunc reads the discriminator as qint16 —
        sending an Int (type id 2) would mis-decode it as a different
        value and the whole frame would be thrown away.
        """
        blob = encode_signalproxy_payload(
            SyncMessage(
                class_name=b"X",
                object_name="",
                slot_name=b"y",
                params=[],
            )
        )
        # The first 4 bytes are the list length (5 items: discriminator,
        # className, objectName, slotName, ...params). Skip past them and
        # check the first element's type id.
        assert blob[:4] == b"\x00\x00\x00\x04"  # 4 items
        # 4-byte type id of the first element = Short (130)
        assert blob[4:8] == b"\x00\x00\x00\x82"


class TestRpcCallRoundtrip:
    def test_basic_roundtrip(self) -> None:
        original = RpcCall(
            signal_name=b"2sendInput(BufferInfo,QString)",
            params=["hello", 42],
        )
        decoded = decode_signalproxy_payload(encode_signalproxy_payload(original))
        assert decoded == original

    def test_rpc_call_with_no_params(self) -> None:
        original = RpcCall(signal_name=b"something", params=[])
        decoded = decode_signalproxy_payload(encode_signalproxy_payload(original))
        assert decoded == original


class TestInitRequestRoundtrip:
    def test_basic_roundtrip(self) -> None:
        original = InitRequest(class_name=b"Network", object_name="5")
        decoded = decode_signalproxy_payload(encode_signalproxy_payload(original))
        assert decoded == original

    def test_empty_object_name(self) -> None:
        original = InitRequest(class_name=b"BufferSyncer", object_name="")
        decoded = decode_signalproxy_payload(encode_signalproxy_payload(original))
        assert decoded == original


class TestInitDataRoundtrip:
    def test_basic_roundtrip(self) -> None:
        original = InitData(
            class_name=b"Network",
            object_name="1",
            init_data={
                "networkName": "freenode",
                "currentServer": "chat.freenode.net",
                "connectionState": 2,
            },
        )
        decoded = decode_signalproxy_payload(encode_signalproxy_payload(original))
        assert isinstance(decoded, InitData)
        assert decoded.class_name == original.class_name
        assert decoded.object_name == original.object_name
        assert decoded.init_data == original.init_data

    def test_empty_init_data(self) -> None:
        original = InitData(class_name=b"X", object_name="", init_data={})
        decoded = decode_signalproxy_payload(encode_signalproxy_payload(original))
        assert decoded == original


class TestHeartBeatRoundtrip:
    def test_heartbeat(self) -> None:
        ts = dt.datetime(2026, 4, 14, 12, 34, 56, 789_000, tzinfo=dt.UTC)
        decoded = decode_signalproxy_payload(encode_signalproxy_payload(HeartBeat(timestamp=ts)))
        assert isinstance(decoded, HeartBeat)
        assert decoded.timestamp == ts

    def test_heartbeat_reply(self) -> None:
        ts = dt.datetime(2026, 4, 14, 12, 34, 56, 789_000, tzinfo=dt.UTC)
        decoded = decode_signalproxy_payload(
            encode_signalproxy_payload(HeartBeatReply(timestamp=ts))
        )
        assert isinstance(decoded, HeartBeatReply)
        assert decoded.timestamp == ts

    def test_heartbeat_and_reply_have_different_discriminators(self) -> None:
        ts = dt.datetime(2026, 4, 14, tzinfo=dt.UTC)
        hb_blob = encode_signalproxy_payload(HeartBeat(timestamp=ts))
        hb_reply_blob = encode_signalproxy_payload(HeartBeatReply(timestamp=ts))
        hb_items = _decode_list(hb_blob)
        hb_reply_items = _decode_list(hb_reply_blob)
        # Heartbeat is 5, HeartBeatReply is 6.
        assert hb_items[0] == REQUEST_HEARTBEAT
        assert hb_reply_items[0] == REQUEST_HEARTBEAT_REPLY


class TestErrorPaths:
    """Malformed frames should raise `SignalProxyError` — desync here is
    unrecoverable, so the connection state machine needs a typed failure to
    react to rather than a decode that silently succeeds with garbage.
    """

    def test_empty_list_raises(self) -> None:
        from quasseltui.qt.datastream import QDataStreamWriter

        writer = QDataStreamWriter()
        writer.write_uint32(0)  # zero-item QVariantList
        with pytest.raises(SignalProxyError, match="empty SignalProxy frame"):
            decode_signalproxy_payload(writer.to_bytes())

    def test_non_int_discriminator_raises(self) -> None:
        from quasseltui.qt.datastream import QDataStreamWriter
        from quasseltui.qt.variant import write_variant

        writer = QDataStreamWriter()
        writer.write_uint32(1)  # one-item list
        write_variant(writer, "not an int")
        with pytest.raises(SignalProxyError, match="discriminator"):
            decode_signalproxy_payload(writer.to_bytes())

    def test_unknown_discriminator_raises(self) -> None:
        from quasseltui.qt.datastream import QDataStreamWriter
        from quasseltui.qt.types import QMetaType
        from quasseltui.qt.variant import write_variant

        writer = QDataStreamWriter()
        writer.write_uint32(1)
        write_variant(writer, 999, type_id=QMetaType.Short)
        with pytest.raises(SignalProxyError, match="unknown SignalProxy discriminator"):
            decode_signalproxy_payload(writer.to_bytes())

    def test_sync_missing_fields_raises(self) -> None:
        from quasseltui.qt.datastream import QDataStreamWriter
        from quasseltui.qt.types import QMetaType
        from quasseltui.qt.variant import write_variant

        writer = QDataStreamWriter()
        writer.write_uint32(2)  # discriminator + className only
        write_variant(writer, REQUEST_SYNC, type_id=QMetaType.Short)
        write_variant(writer, b"Network", type_id=QMetaType.QByteArray)
        with pytest.raises(SignalProxyError, match="Sync:"):
            decode_signalproxy_payload(writer.to_bytes())

    def test_init_data_odd_key_value_pairs_raises(self) -> None:
        from quasseltui.qt.datastream import QDataStreamWriter
        from quasseltui.qt.types import QMetaType
        from quasseltui.qt.variant import write_variant

        writer = QDataStreamWriter()
        writer.write_uint32(4)  # discriminator + class + obj + 1 stray key
        write_variant(writer, REQUEST_INIT_DATA, type_id=QMetaType.Short)
        write_variant(writer, b"X", type_id=QMetaType.QByteArray)
        write_variant(writer, b"", type_id=QMetaType.QByteArray)
        write_variant(writer, b"stranded", type_id=QMetaType.QByteArray)
        with pytest.raises(SignalProxyError, match="must be even"):
            decode_signalproxy_payload(writer.to_bytes())

    def test_init_request_wrong_field_count_raises(self) -> None:
        from quasseltui.qt.datastream import QDataStreamWriter
        from quasseltui.qt.types import QMetaType
        from quasseltui.qt.variant import write_variant

        writer = QDataStreamWriter()
        writer.write_uint32(2)  # discriminator + only className, no object_name
        write_variant(writer, REQUEST_INIT_REQUEST, type_id=QMetaType.Short)
        write_variant(writer, b"X", type_id=QMetaType.QByteArray)
        with pytest.raises(SignalProxyError, match="InitRequest"):
            decode_signalproxy_payload(writer.to_bytes())


class TestSyncByteLayout:
    """A tight pin on a known Sync message: every byte of a
    `Network::setNetworkName("freenode")` frame.

    This is the regression-test equivalent of a wire-format snapshot — if
    the byte layout drifts for any reason (Short id change, QByteArray vs
    QString for className, UTF-16 vs UTF-8 for objectName, ...) this test
    blows up immediately.
    """

    def test_known_blob(self) -> None:
        msg = SyncMessage(
            class_name=b"Network",
            object_name="1",
            slot_name=b"setNetworkName",
            params=["freenode"],
        )
        blob = encode_signalproxy_payload(msg)
        expected = (
            # QVariantList with 5 items
            b"\x00\x00\x00\x05"
            # item 0: QVariant<Short>(1)
            b"\x00\x00\x00\x82"  # type=130 Short
            b"\x00"  # not null
            b"\x00\x01"  # value=1
            # item 1: QVariant<QByteArray>(b"Network")
            b"\x00\x00\x00\x0c"  # type=12 QByteArray
            b"\x00"
            b"\x00\x00\x00\x07"  # 7-byte length
            b"Network"
            # item 2: QVariant<QByteArray>(b"1") (objectName.toUtf8())
            b"\x00\x00\x00\x0c"
            b"\x00"
            b"\x00\x00\x00\x01"
            b"1"
            # item 3: QVariant<QByteArray>(b"setNetworkName")
            b"\x00\x00\x00\x0c"
            b"\x00"
            b"\x00\x00\x00\x0e"
            b"setNetworkName"
            # item 4: QVariant<QString>("freenode") — 16 bytes UTF-16BE
            b"\x00\x00\x00\x0a"  # type=10 QString
            b"\x00"
            b"\x00\x00\x00\x10"  # 16-byte length
            b"\x00f\x00r\x00e\x00e\x00n\x00o\x00d\x00e"
        )
        assert blob == expected


class TestRpcCallByteLayout:
    def test_known_blob(self) -> None:
        msg = RpcCall(signal_name=b"test", params=[42])
        blob = encode_signalproxy_payload(msg)
        expected = (
            b"\x00\x00\x00\x03"  # 3-item list
            # item 0: Short(2 = RpcCall)
            b"\x00\x00\x00\x82"
            b"\x00"
            b"\x00\x02"
            # item 1: QByteArray("test")
            b"\x00\x00\x00\x0c"
            b"\x00"
            b"\x00\x00\x00\x04"
            b"test"
            # item 2: Int(42)
            b"\x00\x00\x00\x02"
            b"\x00"
            b"\x00\x00\x00\x2a"
        )
        assert blob == expected
        # Sanity: and the decoder should get it back.
        decoded = decode_signalproxy_payload(blob)
        assert decoded == msg
        # Sanity: first discriminator matches RpcCall id.
        assert REQUEST_RPC_CALL == 2
