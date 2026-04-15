"""Unit tests for the length-prefixed framing layer.

These tests exercise both the pure byte helpers and the async wrappers.
The async tests use `asyncio.StreamReader` directly with `feed_data` /
`feed_eof` rather than spinning up a fake server — that gives byte-level
control over what the "peer" sends without any socket flakiness.
"""

from __future__ import annotations

import asyncio

import pytest

from quasseltui.protocol.errors import ConnectionClosed, QuasselError
from quasseltui.protocol.framing import (
    FrameTooLargeError,
    encode_frame,
    parse_frame_header,
    read_frame,
    write_frame,
)


class TestPureFrameHelpers:
    def test_encode_frame_prepends_big_endian_length(self) -> None:
        assert encode_frame(b"hello") == b"\x00\x00\x00\x05hello"

    def test_encode_empty_frame(self) -> None:
        assert encode_frame(b"") == b"\x00\x00\x00\x00"

    def test_parse_frame_header_round_trip(self) -> None:
        header = encode_frame(b"x" * 257)[:4]
        assert parse_frame_header(header) == 257

    def test_parse_frame_header_rejects_wrong_length(self) -> None:
        with pytest.raises(QuasselError, match="frame header"):
            parse_frame_header(b"\x00\x00")


class TestReadFrame:
    @pytest.mark.asyncio
    async def test_reads_single_frame(self) -> None:
        reader = _stream_with(encode_frame(b"payload"))
        assert await read_frame(reader) == b"payload"

    @pytest.mark.asyncio
    async def test_reads_two_frames_in_sequence(self) -> None:
        reader = _stream_with(encode_frame(b"first") + encode_frame(b"second"))
        assert await read_frame(reader) == b"first"
        assert await read_frame(reader) == b"second"

    @pytest.mark.asyncio
    async def test_zero_length_frame(self) -> None:
        reader = _stream_with(encode_frame(b""))
        assert await read_frame(reader) == b""

    @pytest.mark.asyncio
    async def test_eof_before_header_raises_connection_closed(self) -> None:
        reader = _stream_with(b"\x00\x00")  # 2 bytes, header needs 4
        with pytest.raises(ConnectionClosed):
            await read_frame(reader)

    @pytest.mark.asyncio
    async def test_eof_mid_payload_raises_connection_closed(self) -> None:
        # Header says 10 bytes, only 3 follow.
        reader = _stream_with(b"\x00\x00\x00\x0aabc")
        with pytest.raises(ConnectionClosed):
            await read_frame(reader)

    @pytest.mark.asyncio
    async def test_oversize_frame_rejected_before_payload_read(self) -> None:
        # Header says 1 MB but the limit is 100 bytes; payload is missing
        # entirely. The limit must reject BEFORE we try to read any payload,
        # otherwise a malicious peer could make us hang waiting on bytes
        # that will never arrive.
        reader = _stream_with(b"\x00\x10\x00\x00")
        with pytest.raises(FrameTooLargeError, match="exceeds max_frame_bytes"):
            await read_frame(reader, max_frame_bytes=100)


class TestWriteFrame:
    @pytest.mark.asyncio
    async def test_writes_and_drains(self) -> None:
        loop = asyncio.get_running_loop()
        sink = _DrainSink(loop)
        writer = _make_writer(sink)
        await write_frame(writer, b"hello")
        assert sink.written == encode_frame(b"hello")
        assert sink.drains == 1

    @pytest.mark.asyncio
    async def test_round_trip_through_pipe(self) -> None:
        # End-to-end: write_frame on one side, read_frame on the other,
        # via a memory pipe so the bytes really travel through asyncio.
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_running_loop()
        sink = _DrainSink(loop)
        writer = _make_writer(sink, protocol=protocol)

        await write_frame(writer, b"ping")
        reader.feed_data(sink.written)
        assert await read_frame(reader) == b"ping"


def _stream_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


class _DrainSink(asyncio.Transport):
    """Minimal transport stand-in that records writes and is_closing=False."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._loop = loop
        self._closed = False
        self.written = bytearray()
        self.drains = 0

    def write(self, data: bytes | bytearray | memoryview) -> None:  # type: ignore[override]
        self.written.extend(data)

    def is_closing(self) -> bool:  # type: ignore[override]
        return self._closed

    def close(self) -> None:  # type: ignore[override]
        self._closed = True

    def get_write_buffer_size(self) -> int:  # type: ignore[override]
        return 0


def _make_writer(
    transport: _DrainSink,
    *,
    protocol: asyncio.StreamReaderProtocol | None = None,
) -> asyncio.StreamWriter:
    if protocol is None:
        # A bare protocol is enough for write-only flows; the writer never
        # touches the reader half.
        protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
    loop = asyncio.get_running_loop()
    writer = asyncio.StreamWriter(transport, protocol, reader=None, loop=loop)
    # `drain()` consults the protocol's drain helper, which checks
    # `_paused`; with no real flow control we just count drains by
    # monkey-patching.
    original_drain = writer.drain

    async def _drain_counting() -> None:
        transport.drains += 1
        await original_drain()

    writer.drain = _drain_counting  # type: ignore[method-assign]
    return writer
