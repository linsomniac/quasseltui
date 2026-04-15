"""Capture a live Quassel session to disk for offline replay tests.

This is a developer tool, not a runtime dependency: it runs the same probe
+ handshake the real CLI runs, but tees every byte sent and received to
two files (`<prefix>.sent.bin` and `<prefix>.recv.bin`) so we can build
byte-fixture regression tests without needing a live core in CI.

Usage (phase 2 — probe + ClientInit only):

    uv run python tests/tools/capture_session.py \\
        --host core.example.org --port 4242 \\
        --prefix tests/fixtures/probe_and_init

Usage (phase 3 — full handshake through SessionInit):

    QUASSEL_PASSWORD=hunter2 uv run python tests/tools/capture_session.py \\
        --host core.example.org --port 4242 --user sean --login \\
        --prefix tests/fixtures/session_init

The output files are append-only — re-run with `--clean` to start fresh.
After TLS upgrade we can no longer tee the actual TCP bytes (they're
inside the tunnel), so we log the plaintext frames as they'd appear on an
unencrypted core. That's the format the offline replay tests need anyway.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import os
import sys
from pathlib import Path

from quasseltui import __version__
from quasseltui.protocol.errors import AuthError, QuasselError
from quasseltui.protocol.framing import read_frame, write_frame
from quasseltui.protocol.handshake import (
    decode_handshake_payload,
    encode_client_init,
    encode_client_login,
)
from quasseltui.protocol.messages import (
    ClientInit,
    ClientInitAck,
    ClientInitReject,
    ClientLogin,
    ClientLoginAck,
    SessionInit,
    parse_handshake_message,
)
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
    parser.add_argument(
        "--login",
        action="store_true",
        help=(
            "Continue past ClientInitAck through ClientLogin and "
            "SessionInit. Requires --user (or QUASSEL_USER)."
        ),
    )
    parser.add_argument(
        "--user",
        help="Username for ClientLogin (env: QUASSEL_USER).",
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

        # ClientInit (framed)
        init_payload = encode_client_init(
            ClientInit(client_version=f"quasseltui v{__version__}", build_date="2026-04-14"),
        )
        sent_log.write(_framed(init_payload))
        await write_frame(writer, init_payload)

        ack_payload = await read_frame(reader)
        recv_log.write(_framed(ack_payload))
        ack = parse_handshake_message(decode_handshake_payload(ack_payload))
        print(f"received: {type(ack).__name__}")

        if not args.login:
            return 0
        if isinstance(ack, ClientInitReject):
            print(f"core rejected ClientInit: {ack.error_string!r}", file=sys.stderr)
            return 2
        if not isinstance(ack, ClientInitAck):
            print(
                f"unexpected handshake reply at init phase: {type(ack).__name__}",
                file=sys.stderr,
            )
            return 2
        if not ack.configured:
            print("core is not configured — cannot login", file=sys.stderr)
            return 2

        user = args.user or os.environ.get("QUASSEL_USER")
        if not user:
            print("--login requires --user or QUASSEL_USER", file=sys.stderr)
            return 1
        password = os.environ.get("QUASSEL_PASSWORD")
        if password is None:
            try:
                password = getpass.getpass(f"Password for {user}@{args.host}: ")
            except (EOFError, KeyboardInterrupt):
                print("\naborted at password prompt", file=sys.stderr)
                return 1

        login_payload = encode_client_login(ClientLogin(user=user, password=password))
        sent_log.write(_framed(login_payload))
        await write_frame(writer, login_payload)

        try:
            login_ack_payload = await read_frame(reader)
            recv_log.write(_framed(login_ack_payload))
            login_ack = parse_handshake_message(decode_handshake_payload(login_ack_payload))
        except AuthError as exc:
            print(f"login rejected: {exc}", file=sys.stderr)
            return 3

        if not isinstance(login_ack, ClientLoginAck):
            print(
                f"expected ClientLoginAck, got {type(login_ack).__name__}",
                file=sys.stderr,
            )
            return 2
        print("received: ClientLoginAck")

        session_payload = await read_frame(reader)
        recv_log.write(_framed(session_payload))
        session = parse_handshake_message(decode_handshake_payload(session_payload))
        if not isinstance(session, SessionInit):
            print(
                f"expected SessionInit, got {type(session).__name__}",
                file=sys.stderr,
            )
            return 2
        print(
            f"received: SessionInit "
            f"({len(session.identities)} identities, "
            f"{len(session.network_ids)} networks, "
            f"{len(session.buffer_infos)} buffers)"
        )
        return 0
    except QuasselError as exc:
        print(f"protocol error: {exc}", file=sys.stderr)
        return 1
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        sent_log.close()
        recv_log.close()
        print(f"wrote {sent_path}, {recv_path}")


def _framed(payload: bytes) -> bytes:
    """Reproduce the framed-on-the-wire shape of `payload` (length prefix + body)."""
    return len(payload).to_bytes(4, "big") + payload


if __name__ == "__main__":
    sys.exit(main())
