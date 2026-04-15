"""Command-line entry point for quasseltui.

Phase 2 wires up `--probe-only`: open a socket to a real Quassel core, run
the probe handshake, optionally upgrade to TLS, send a `ClientInit`, decode
the reply, pretty-print it, and exit cleanly. This is the first command
that actually talks to a live core and will be the workhorse for capturing
byte fixtures during phase development.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from quasseltui import __version__
from quasseltui.protocol.errors import QuasselError
from quasseltui.protocol.handshake import recv_handshake_message, send_client_init
from quasseltui.protocol.messages import ClientInit, ClientInitAck, ClientInitReject
from quasseltui.protocol.probe import ConnectionFeature, NegotiatedProtocol, probe
from quasseltui.protocol.transport import (
    TlsOptions,
    TransportError,
    close_writer,
    open_tcp_connection,
    start_tls_on_writer,
)

CLIENT_VERSION = f"quasseltui v{__version__}"
BUILD_DATE = "2026-04-14"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quasseltui",
        description="Terminal client for Quassel IRC cores.",
    )
    parser.add_argument("--version", action="version", version=f"quasseltui {__version__}")

    sub = parser.add_subparsers(dest="mode")

    probe_only = sub.add_parser(
        "probe-only",
        help="Run the probe + ClientInit handshake against a core, print the reply, and exit.",
    )
    probe_only.add_argument("--host", required=True, help="Quassel core hostname or IP")
    probe_only.add_argument("--port", type=int, required=True, help="Quassel core port")
    probe_only.add_argument(
        "--no-tls",
        action="store_true",
        help="Do not offer encryption during the probe (plain TCP only).",
    )
    probe_only.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (self-signed cores).",
    )
    probe_only.add_argument(
        "--cafile",
        help="Path to a PEM bundle of trust anchors to use during TLS verification.",
    )
    probe_only.add_argument(
        "--connect-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the TCP connect (default: 10).",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.mode == "probe-only":
        return asyncio.run(_probe_only(args))

    print(f"quasseltui {__version__} — under construction")
    return 0


async def _probe_only(args: argparse.Namespace) -> int:
    """Run a single probe+ClientInit round trip and print the reply."""
    try:
        reader, writer = await open_tcp_connection(
            args.host, args.port, connect_timeout=args.connect_timeout
        )
    except TransportError as exc:
        print(f"connect: {exc}", file=sys.stderr)
        return 2

    try:
        offered = ConnectionFeature.NONE if args.no_tls else ConnectionFeature.Encryption
        negotiated = await probe(reader, writer, offered_features=offered)
        _print_negotiated(negotiated)

        if negotiated.tls_required:
            await start_tls_on_writer(
                writer,
                host=args.host,
                options=TlsOptions(verify=not args.insecure, cafile=args.cafile),
            )
            print("TLS upgrade ok")
        elif not args.no_tls:
            print("WARNING: core did not enable TLS — credentials would be sent in plaintext")

        await send_client_init(
            writer,
            ClientInit(client_version=CLIENT_VERSION, build_date=BUILD_DATE),
        )

        reply = await recv_handshake_message(reader)
        _print_reply(reply)
        return 0 if isinstance(reply, ClientInitAck) else 3
    except QuasselError as exc:
        print(f"protocol: {exc}", file=sys.stderr)
        return 4
    finally:
        await close_writer(writer)


def _print_negotiated(n: NegotiatedProtocol) -> None:
    print(f"protocol:    {n.protocol.name}")
    print(f"peer feats:  {n.peer_features:#06x}")
    print(f"conn feats:  {n.connection_features!r}")


def _print_reply(reply: ClientInitAck | ClientInitReject) -> None:
    if isinstance(reply, ClientInitReject):
        print(f"core REJECTED ClientInit: {reply.error_string!r}")
        return

    print("core accepted ClientInit:")
    print(f"  configured:    {reply.configured}")
    print(f"  core features: {reply.core_features:#010x}")
    if reply.feature_list:
        print(f"  feature list:  {', '.join(reply.feature_list)}")
    if reply.protocol_version is not None:
        print(f"  proto version: {reply.protocol_version}")
    if reply.storage_backends:
        print("  storage backends:")
        for b in reply.storage_backends:
            print(f"    - {b.display_name}: {b.description}")
    if reply.authenticators:
        print("  authenticators:")
        for a in reply.authenticators:
            print(f"    - {a.display_name}: {a.description}")
