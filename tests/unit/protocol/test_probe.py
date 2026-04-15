"""Unit tests for the Quassel probe handshake.

The probe wire format is fully pinned by these tests against hand-built
byte blobs derived from the Quassel C++ source. The exact bit layout is
the most-likely place to silently produce something a real core will
quietly reject — so we assert byte-for-byte rather than just round-tripping.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from quasseltui.protocol.errors import ProbeError
from quasseltui.protocol.probe import (
    ConnectionFeature,
    NegotiatedProtocol,
    ProtocolType,
    build_probe_request,
    parse_probe_reply,
    probe,
)


class TestBuildProbeRequest:
    def test_default_request_offers_encryption_and_datastream(self) -> None:
        # magic = 0x42b33f00 | 0x01 (Encryption) = 0x42b33f01
        # protos = [DataStream(0x02) | end-of-list bit] = 0x80000002
        expected = struct.pack(">II", 0x42B33F01, 0x80000002)
        assert build_probe_request() == expected

    def test_no_features_offered(self) -> None:
        expected = struct.pack(">II", 0x42B33F00, 0x80000002)
        assert build_probe_request(offered_features=ConnectionFeature.NONE) == expected

    def test_compression_and_encryption_offered(self) -> None:
        expected = struct.pack(">II", 0x42B33F03, 0x80000002)
        got = build_probe_request(
            offered_features=ConnectionFeature.Encryption | ConnectionFeature.Compression
        )
        assert got == expected

    def test_multiple_protocols_marks_only_last(self) -> None:
        # First entry is plain DataStream with feature bits 0x0042;
        # second entry is a hypothetical legacy fallback with end-bit set.
        # The middle bytes (8-23) hold proto features.
        got = build_probe_request(
            offered_features=ConnectionFeature.NONE,
            protocols=(
                (ProtocolType.DataStream, 0x0042),
                (ProtocolType.Legacy, 0x0000),
            ),
        )
        # magic, datastream-with-features (no end bit), legacy-with-end-bit
        expected = struct.pack(
            ">III",
            0x42B33F00,
            0x00004202,  # DataStream(0x02) | (0x42 << 8)
            0x80000001,  # Legacy(0x01) | end-bit
        )
        assert got == expected

    def test_empty_protocol_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one protocol"):
            build_probe_request(protocols=())

    def test_oversize_protocol_features_rejected(self) -> None:
        with pytest.raises(ValueError, match="16 bits"):
            build_probe_request(protocols=((ProtocolType.DataStream, 0x10000),))


class TestParseProbeReply:
    def test_datastream_with_tls(self) -> None:
        # Reply layout: low byte = proto, bits 8-23 = peer features, bits 24-31 = conn features
        # DataStream(0x02), peer feats=0x1234, conn feats=0x01 (Encryption)
        word = 0x02 | (0x1234 << 8) | (0x01 << 24)
        reply = struct.pack(">I", word)
        n = parse_probe_reply(reply)
        assert n == NegotiatedProtocol(
            protocol=ProtocolType.DataStream,
            peer_features=0x1234,
            connection_features=ConnectionFeature.Encryption,
        )
        assert n.tls_required
        assert not n.compression_enabled

    def test_datastream_no_features(self) -> None:
        reply = struct.pack(">I", 0x00000002)
        n = parse_probe_reply(reply)
        assert n.protocol is ProtocolType.DataStream
        assert n.peer_features == 0
        assert n.connection_features == ConnectionFeature.NONE
        assert not n.tls_required

    def test_compression_alone_rejected(self) -> None:
        # Compression is in the supported-bits set but we deliberately
        # don't ship a decompressor, so any reply that asserts the
        # Compression bit must be rejected — even (especially!) when the
        # Encryption bit is absent.
        word = 0x02 | (0x02 << 24)
        with pytest.raises(ProbeError, match="Compression"):
            parse_probe_reply(
                struct.pack(">I", word),
                offered_features=ConnectionFeature.Encryption | ConnectionFeature.Compression,
            )

    def test_wrong_length_rejected(self) -> None:
        with pytest.raises(ProbeError, match="4 bytes"):
            parse_probe_reply(b"\x00\x00")

    def test_unknown_protocol_rejected(self) -> None:
        # 0xff is not in the ProtocolType enum.
        with pytest.raises(ProbeError, match="unknown protocol type"):
            parse_probe_reply(struct.pack(">I", 0x000000FF))

    def test_legacy_protocol_rejected(self) -> None:
        # We only speak DataStream; if a core picks Legacy that's a hard fail.
        with pytest.raises(ProbeError, match="Legacy"):
            parse_probe_reply(struct.pack(">I", 0x00000001))

    def test_internal_protocol_rejected(self) -> None:
        # The Internal protocol is for in-process Quassel monolithic mode and
        # is never something a real client should be presented with.
        with pytest.raises(ProbeError, match="Internal"):
            parse_probe_reply(struct.pack(">I", 0x00000000))

    def test_unoffered_features_rejected(self) -> None:
        # We offered NONE; the core asserts Encryption. A hostile peer doing
        # this could try to push us into a confused TLS handshake.
        word = 0x02 | (0x01 << 24)
        with pytest.raises(ProbeError, match="did not offer"):
            parse_probe_reply(
                struct.pack(">I", word),
                offered_features=ConnectionFeature.NONE,
            )

    def test_unknown_feature_bits_rejected(self) -> None:
        # Bits 0x04 / 0x08 / ... in the conn-features byte are not defined
        # by the Protocol::Feature enum at all. A buggy peer asserting them
        # is a protocol error, not something we should silently mask.
        word = 0x02 | (0x80 << 24)  # 0x80 is not Encryption or Compression
        with pytest.raises(ProbeError, match="unknown connection feature"):
            parse_probe_reply(
                struct.pack(">I", word),
                offered_features=ConnectionFeature.Encryption,
            )


class TestProbeAsync:
    @pytest.mark.asyncio
    async def test_round_trip_via_memory_pipe(self) -> None:
        """End-to-end: drive `probe()` against a fake peer that records the
        request bytes and feeds back a canned reply. Ensures we both write
        the right thing and parse the right thing in one shot."""
        reader = asyncio.StreamReader()
        # Reply: DataStream + Encryption
        reply_word = 0x02 | (0x01 << 24)
        reader.feed_data(struct.pack(">I", reply_word))
        reader.feed_eof()

        recorded = bytearray()
        writer = _writer_recording(recorded)

        n = await probe(reader, writer)
        assert n.protocol is ProtocolType.DataStream
        assert n.tls_required
        # And the request we sent is the canonical default-shaped probe.
        assert bytes(recorded) == build_probe_request()


def _writer_recording(sink: bytearray) -> asyncio.StreamWriter:
    """Build a `StreamWriter` whose writes append to `sink` and whose
    `drain()` is a no-op coroutine. Just enough for `probe()`."""

    class _Sink(asyncio.Transport):
        def __init__(self) -> None:
            super().__init__()
            self._closed = False

        def write(self, data: bytes | bytearray | memoryview) -> None:  # type: ignore[override]
            sink.extend(data)

        def is_closing(self) -> bool:  # type: ignore[override]
            return self._closed

        def close(self) -> None:  # type: ignore[override]
            self._closed = True

        def get_write_buffer_size(self) -> int:  # type: ignore[override]
            return 0

    loop = asyncio.get_running_loop()
    transport = _Sink()
    protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
    writer = asyncio.StreamWriter(transport, protocol, reader=None, loop=loop)

    async def _no_drain() -> None:
        return None

    writer.drain = _no_drain  # type: ignore[method-assign]
    return writer
