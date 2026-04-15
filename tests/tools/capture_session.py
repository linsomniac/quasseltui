"""Capture a live Quassel session to disk for offline replay tests.

This is a developer tool, not a runtime dependency: it runs the same probe
+ ClientInit handshake the real CLI runs, but tees every byte sent and
received to two files (`<prefix>.sent.bin` and `<prefix>.recv.bin`) so we
can build byte-fixture regression tests without needing a live core in CI.

Usage:

    uv run python tests/tools/capture_session.py \\
        --host core.example.org --port 4242 --prefix tests/fixtures/probe_and_init

The output files are append-only — re-run with `--clean` to start fresh.

Phase 2 only captures the probe + ClientInit/Ack exchange. Later phases
will extend this same script to also drive ClientLogin and a few seconds
of CONNECTED-state traffic.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from quasseltui import __version__
from quasseltui.protocol.errors import QuasselError
from quasseltui.protocol.framing import read_frame, write_frame
from quasseltui.protocol.handshake import (
    decode_handshake_payload,
    encode_client_init,
)
from quasseltui.protocol.messages import ClientInit, parse_handshake_message
from quasseltui.protocol.probe import ConnectionFeature, build_probe_request, parse_probe_reply
from quasseltui.protocol.transport import (
    TlsOptions,
    open_tcp_connection,
    start_tls_on_writer,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--prefix",
        required=True,
        help="Path prefix for the .sent.bin / .recv.bin files",
    )
    parser.add_argument("--no-tls", action="store_true")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Truncate the output files before writing.",
    )
    args = parser.parse_args()

    sent_path = Path(f"{args.prefix}.sent.bin")
    recv_path = Path(f"{args.prefix}.recv.bin")
    sent_path.parent.mkdir(parents=True, exist_ok=True)
    if args.clean:
        sent_path.unlink(missing_ok=True)
        recv_path.unlink(missing_ok=True)

    return asyncio.run(_capture(args, sent_path, recv_path))


async def _capture(args: argparse.Namespace, sent_path: Path, recv_path: Path) -> int:
    sent_log = sent_path.open("ab")
    recv_log = recv_path.open("ab")
    try:
        reader, writer = await open_tcp_connection(args.host, args.port)

        # Probe (raw, unframed)
        offered = ConnectionFeature.NONE if args.no_tls else ConnectionFeature.Encryption
        probe_bytes = build_probe_request(offered_features=offered)
        sent_log.write(probe_bytes)
        writer.write(probe_bytes)
        await writer.drain()

        reply = await reader.readexactly(4)
        recv_log.write(reply)
        negotiated = parse_probe_reply(reply)
        print(f"probe: {negotiated.protocol.name} feats={negotiated.connection_features!r}")

        if negotiated.tls_required:
            await start_tls_on_writer(
                writer,
                host=args.host,
                options=TlsOptions(verify=not args.insecure),
            )
            print("TLS upgraded")

        # ClientInit (framed). After TLS, we cannot tee the actual TCP bytes
        # because they're inside the TLS tunnel — so we tee the *plaintext*
        # frame payload and length prefix as it would appear on the wire of
        # an unencrypted core. That's the format the offline replay tests
        # need anyway.
        init_payload = encode_client_init(
            ClientInit(client_version=f"quasseltui v{__version__}", build_date="2026-04-14"),
        )
        framed = (len(init_payload).to_bytes(4, "big")) + init_payload
        sent_log.write(framed)
        await write_frame(writer, init_payload)

        ack_payload = await read_frame(reader)
        framed_ack = (len(ack_payload).to_bytes(4, "big")) + ack_payload
        recv_log.write(framed_ack)
        decoded = decode_handshake_payload(ack_payload)
        msg = parse_handshake_message(decoded)
        print(f"received: {type(msg).__name__}")

        writer.close()
        await writer.wait_closed()
        return 0
    except QuasselError as exc:
        print(f"protocol error: {exc}", file=sys.stderr)
        return 1
    finally:
        sent_log.close()
        recv_log.close()
        print(f"wrote {sent_path}, {recv_path}")


if __name__ == "__main__":
    sys.exit(main())
