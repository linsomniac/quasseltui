"""Command-line entry point for quasseltui.

Two diagnostic subcommands at the moment:

- `probe-only` (phase 2) runs the probe handshake, optional TLS upgrade,
  `ClientInit` round-trip, and exits. Useful for sanity-checking that a
  core is reachable and what features it advertises.
- `login-only` (phase 3) does everything `probe-only` does and then sends
  `ClientLogin`, waits for `SessionInit`, and pretty-prints a summary of
  identities, networks, and buffers. This is the workhorse for capturing
  byte fixtures during phase development and the first command that needs
  actual credentials.

Both modes are headless and exit when done — the Textual UI lands in
phase 6+.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from collections import defaultdict
from collections.abc import Sequence

from quasseltui import __version__
from quasseltui.protocol.errors import AuthError, QuasselError
from quasseltui.protocol.handshake import (
    recv_handshake_message,
    send_client_init,
    send_client_login,
)
from quasseltui.protocol.messages import (
    ClientInit,
    ClientInitAck,
    ClientInitReject,
    ClientLogin,
    ClientLoginAck,
    CoreSetupReject,
    SessionInit,
)
from quasseltui.protocol.probe import ConnectionFeature, NegotiatedProtocol, probe
from quasseltui.protocol.transport import (
    TlsOptions,
    TransportError,
    close_writer,
    open_tcp_connection,
    start_tls_on_writer,
)
from quasseltui.protocol.usertypes import BufferInfo, BufferType

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
        help=(
            "Do not offer encryption during the probe (plain TCP only). "
            "Implies --allow-plaintext. Use only against trusted local cores."
        ),
    )
    probe_only.add_argument(
        "--allow-plaintext",
        action="store_true",
        help=(
            "Allow the session to continue if the core does not enable TLS "
            "even though we offered it. Without this flag we abort to "
            "prevent a downgrade attack from leaking credentials."
        ),
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

    login_only = sub.add_parser(
        "login-only",
        help=(
            "Run the full handshake (probe + ClientInit + ClientLogin) "
            "against a core, print the SessionInit summary, and exit."
        ),
    )
    login_only.add_argument("--host", required=True, help="Quassel core hostname or IP")
    login_only.add_argument("--port", type=int, required=True, help="Quassel core port")
    login_only.add_argument(
        "--user",
        help="Username (env: QUASSEL_USER; prompted if neither is set)",
    )
    login_only.add_argument(
        "--password",
        help=(
            "Password — discouraged on the command line because it shows up "
            "in shell history and `ps`. Prefer the QUASSEL_PASSWORD env var "
            "or be prompted interactively."
        ),
    )
    login_only.add_argument(
        "--no-tls",
        action="store_true",
        help=(
            "Do not offer encryption during the probe (plain TCP only). "
            "WARNING: your password goes over the wire in plaintext. Use "
            "only against trusted local cores."
        ),
    )
    login_only.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (self-signed cores).",
    )
    login_only.add_argument(
        "--cafile",
        help="Path to a PEM bundle of trust anchors to use during TLS verification.",
    )
    login_only.add_argument(
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
    if args.mode == "login-only":
        return asyncio.run(_login_only(args))

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
        elif not args.no_tls and not args.allow_plaintext:
            # We offered Encryption and the core's reply did not enable it.
            # An active MITM can strip the bit because the probe reply is
            # unauthenticated until TLS starts. Fail closed instead of
            # leaking ClientInit (and eventually ClientLogin) in plaintext.
            print(
                "abort: core did not enable TLS but we offered it. This is a "
                "downgrade and could be a MITM. Re-run with --allow-plaintext "
                "if you actually trust this network path.",
                file=sys.stderr,
            )
            return 5

        await send_client_init(
            writer,
            ClientInit(client_version=CLIENT_VERSION, build_date=BUILD_DATE),
        )

        reply = await recv_handshake_message(reader)
        if not isinstance(reply, ClientInitAck | ClientInitReject):
            # `recv_handshake_message` returns the full handshake-message
            # union, but at this point in the conversation only an Ack or
            # Reject is valid. Anything else means the core sent a message
            # out of order — which is a protocol-level bug we want to
            # surface rather than mis-print.
            print(
                f"unexpected handshake reply at init phase: {type(reply).__name__}",
                file=sys.stderr,
            )
            return 4
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


async def _login_only(args: argparse.Namespace) -> int:
    """Run the full handshake (probe + ClientInit + ClientLogin) and stop.

    Exit codes:
        0 — fully connected through SessionInit
        1 — bad arguments / missing creds
        2 — TCP connect failed
        3 — core sent ClientInitReject
        4 — protocol-level error
        5 — TLS downgrade detected (we offered, core didn't enable)
        6 — core not configured (would need CoreSetupData; not supported)
        7 — auth rejected (bad user/password)
    """
    user = args.user or os.environ.get("QUASSEL_USER")
    if not user:
        print("login-only: --user or QUASSEL_USER is required", file=sys.stderr)
        return 1

    password = args.password or os.environ.get("QUASSEL_PASSWORD")
    if password is None:
        try:
            password = getpass.getpass(f"Password for {user}@{args.host}: ")
        except (EOFError, KeyboardInterrupt):
            print("\nlogin-only: aborted at password prompt", file=sys.stderr)
            return 1
    if not password:
        print("login-only: empty password not allowed", file=sys.stderr)
        return 1

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
            # Same TLS-downgrade defense as probe-only, but with no
            # `--allow-plaintext` escape hatch: this command sends
            # credentials so we MUST refuse to continue without TLS unless
            # the user explicitly asked for plaintext via --no-tls.
            print(
                "abort: core did not enable TLS but we offered it. This is a "
                "downgrade and would leak the password. Re-run with --no-tls "
                "if you actually trust this network path.",
                file=sys.stderr,
            )
            return 5

        await send_client_init(
            writer,
            ClientInit(client_version=CLIENT_VERSION, build_date=BUILD_DATE),
        )

        init_ack = await recv_handshake_message(reader)
        if isinstance(init_ack, ClientInitReject):
            _print_reply(init_ack)
            return 3
        if not isinstance(init_ack, ClientInitAck):
            print(
                f"unexpected handshake message during init phase: {type(init_ack).__name__}",
                file=sys.stderr,
            )
            return 4
        _print_reply(init_ack)
        if not init_ack.configured:
            print(
                "abort: core is not configured yet. quasseltui does not "
                "implement the CoreSetupData wizard — finish setup in "
                "quasselclient first.",
                file=sys.stderr,
            )
            return 6

        await send_client_login(writer, ClientLogin(user=user, password=password))

        try:
            login_ack = await recv_handshake_message(reader)
        except AuthError as exc:
            print(f"login rejected: {exc}", file=sys.stderr)
            return 7

        if isinstance(login_ack, CoreSetupReject):
            print(f"core setup rejected: {login_ack.error_string!r}", file=sys.stderr)
            return 6
        if not isinstance(login_ack, ClientLoginAck):
            print(
                f"unexpected handshake message during login phase: {type(login_ack).__name__}",
                file=sys.stderr,
            )
            return 4
        print("login ok")

        session = await recv_handshake_message(reader)
        if not isinstance(session, SessionInit):
            print(
                f"expected SessionInit, got {type(session).__name__}",
                file=sys.stderr,
            )
            return 4
        _print_session_init(session)
        return 0
    except QuasselError as exc:
        print(f"protocol: {exc}", file=sys.stderr)
        return 4
    finally:
        await close_writer(writer)


def _print_session_init(session: SessionInit) -> None:
    print(
        f"connected — {len(session.identities)} identities, "
        f"{len(session.network_ids)} networks, "
        f"{len(session.buffer_infos)} buffers"
    )

    if session.identities:
        print("identities:")
        for ident in session.identities:
            name = ident.get("identityName") or ident.get("IdentityName") or "?"
            ident_id = ident.get("identityId") or ident.get("IdentityId")
            print(f"  - {name} (id={ident_id})")

    if session.network_ids:
        print("networks:")
        for nid in sorted(session.network_ids, key=int):
            print(f"  - network_id={int(nid)}")

    if session.buffer_infos:
        print("buffers:")
        by_network: dict[int, list[BufferInfo]] = defaultdict(list)
        for buf in session.buffer_infos:
            by_network[int(buf.network_id)].append(buf)
        for net_id in sorted(by_network):
            buffers = by_network[net_id]
            print(f"  network_id={net_id} ({len(buffers)} buffers)")
            for buf in sorted(buffers, key=lambda b: (b.type.value, b.name.lower())):
                kind = _buffer_type_label(buf.type)
                print(f"    [{kind}] {buf.name or '(unnamed)'} (buffer_id={int(buf.buffer_id)})")


def _buffer_type_label(t: BufferType) -> str:
    return {
        BufferType.Status: "status",
        BufferType.Channel: "chan",
        BufferType.Query: "query",
        BufferType.Group: "group",
        BufferType.Invalid: "?",
    }.get(t, "?")
