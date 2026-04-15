"""Command-line entry point for quasseltui.

Six subcommands at the moment:

- `probe-only` (phase 2) runs the probe handshake, optional TLS upgrade,
  `ClientInit` round-trip, and exits. Useful for sanity-checking that a
  core is reachable and what features it advertises.
- `login-only` (phase 3) does everything `probe-only` does and then sends
  `ClientLogin`, waits for `SessionInit`, and pretty-prints a summary of
  identities, networks, and buffers. This is the workhorse for capturing
  byte fixtures during phase development and the first command that needs
  actual credentials.
- `stream-only` (phase 4) builds on `login-only` by then entering the
  CONNECTED state and pretty-printing every `SignalProxy` event the core
  sends until `--duration` seconds elapse. Useful for capturing a live
  byte stream (e.g. to feed `tests/fixtures/connected_stream.bin`) and
  sanity-checking the heartbeat reply path.
- `dump-state` (phase 5) runs the embeddable `QuasselClient` stack for
  `--duration` seconds and prints the populated `ClientState` snapshot
  when it's done. This validates the sync/dispatcher + state layers
  end-to-end against a real core.
- `ui-demo` (phase 6) launches the Textual TUI against a hand-built
  static `ClientState` — no network, no credentials. Useful for
  eyeballing the layout without a core.
- `ui` (phase 7) launches the Textual TUI against a live Quassel core.
  This is the actual interactive client: it runs the full handshake,
  streams events through `ClientBridge`, and paints the UI as state
  updates. Requires the same credentials/TLS arguments as `dump-state`.

The first four modes are headless and exit when done; `ui-demo` and
`ui` start an interactive Textual app and exit on `Ctrl+Q`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from collections import defaultdict
from collections.abc import Sequence

from quasseltui import __version__
from quasseltui.client import ClientState, QuasselClient
from quasseltui.client.events import (
    Disconnected as ClientDisconnected,
)
from quasseltui.protocol.connection import (
    Disconnected,
    HeartBeatEvent,
    InitDataEvent,
    InitRequestEvent,
    ProtocolEvent,
    QuasselConnection,
    RpcEvent,
    SessionReady,
    SyncEvent,
)
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
from quasseltui.util.text import sanitize_terminal as _sanitize_terminal

CLIENT_VERSION = f"quasseltui v{__version__}"
BUILD_DATE = "2026-04-14"


def _add_core_connect_args(
    sub: argparse._ArgumentGroup | argparse.ArgumentParser,
    *,
    sends_credentials: bool,
    include_allow_plaintext: bool,
) -> None:
    """Attach the host/port/TLS/timeout args every diagnostic mode shares.

    `sends_credentials=True` makes the `--no-tls` warning blunter and is
    documentation-only — the actual fail-closed policy is enforced in the
    handler. `include_allow_plaintext=True` is only for `probe-only`, which
    doesn't send credentials and thus doesn't need to fail-closed on TLS
    downgrade.
    """
    sub.add_argument("--host", required=True, help="Quassel core hostname or IP")
    sub.add_argument("--port", type=int, required=True, help="Quassel core port")
    tls_help = (
        "Do not offer encryption during the probe (plain TCP only). "
        "WARNING: your password goes over the wire in plaintext. Use "
        "only against trusted local cores."
        if sends_credentials
        else "Do not offer encryption during the probe (plain TCP only). "
        "Implies --allow-plaintext. Use only against trusted local cores."
    )
    sub.add_argument("--no-tls", action="store_true", help=tls_help)
    if include_allow_plaintext:
        sub.add_argument(
            "--allow-plaintext",
            action="store_true",
            help=(
                "Allow the session to continue if the core does not enable TLS "
                "even though we offered it. Without this flag we abort to "
                "prevent a downgrade attack from leaking credentials."
            ),
        )
    sub.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (self-signed cores).",
    )
    sub.add_argument(
        "--cafile",
        help="Path to a PEM bundle of trust anchors to use during TLS verification.",
    )
    sub.add_argument(
        "--connect-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the TCP connect (default: 10).",
    )


def _add_credential_args(sub: argparse.ArgumentParser) -> None:
    """Attach `--user` / `--password` (login-only and stream-only share these)."""
    sub.add_argument(
        "--user",
        help="Username (env: QUASSEL_USER; prompted if neither is set)",
    )
    sub.add_argument(
        "--password",
        help=(
            "Password — discouraged on the command line because it shows up "
            "in shell history and `ps`. Prefer the QUASSEL_PASSWORD env var "
            "or be prompted interactively."
        ),
    )


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
    _add_core_connect_args(
        probe_only,
        sends_credentials=False,
        include_allow_plaintext=True,
    )

    login_only = sub.add_parser(
        "login-only",
        help=(
            "Run the full handshake (probe + ClientInit + ClientLogin) "
            "against a core, print the SessionInit summary, and exit."
        ),
    )
    _add_core_connect_args(
        login_only,
        sends_credentials=True,
        include_allow_plaintext=False,
    )
    _add_credential_args(login_only)

    stream_only = sub.add_parser(
        "stream-only",
        help=(
            "Run the full handshake, then stream SignalProxy events from "
            "the core for --duration seconds. Pretty-prints each event."
        ),
    )
    _add_core_connect_args(
        stream_only,
        sends_credentials=True,
        include_allow_plaintext=False,
    )
    _add_credential_args(stream_only)
    stream_only.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="Seconds to stream events after handshake (default: 60).",
    )
    stream_only.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="Optional cap on how many events to print before exiting.",
    )
    stream_only.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print the raw params/init_data on each event (may be long).",
    )

    dump_state = sub.add_parser(
        "dump-state",
        help=(
            "Run the full handshake and QuasselClient stack for --duration "
            "seconds, then print a snapshot of ClientState (networks, "
            "buffers, messages, identities)."
        ),
    )
    _add_core_connect_args(
        dump_state,
        sends_credentials=True,
        include_allow_plaintext=False,
    )
    _add_credential_args(dump_state)
    dump_state.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="Seconds to accumulate state before dumping (default: 30).",
    )
    dump_state.add_argument(
        "--max-messages",
        type=int,
        default=5,
        help="Maximum messages per buffer to print in the summary (default: 5).",
    )

    sub.add_parser(
        "ui-demo",
        help=(
            "Launch the Textual UI against static placeholder data. "
            "Useful for eyeballing the layout without needing a core. "
            "Press Ctrl+Q to quit."
        ),
    )

    ui = sub.add_parser(
        "ui",
        help=(
            "Launch the Textual UI against a live Quassel core. Runs "
            "the full handshake, streams events, and paints the UI as "
            "the client state updates. Press Ctrl+Q to quit."
        ),
    )
    _add_core_connect_args(
        ui,
        sends_credentials=True,
        include_allow_plaintext=False,
    )
    _add_credential_args(ui)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.mode == "probe-only":
        return asyncio.run(_probe_only(args))
    if args.mode == "login-only":
        return asyncio.run(_login_only(args))
    if args.mode == "stream-only":
        return asyncio.run(_stream_only(args))
    if args.mode == "dump-state":
        return asyncio.run(_dump_state(args))
    if args.mode == "ui-demo":
        return _ui_demo(args)
    if args.mode == "ui":
        return _ui(args)

    print(f"quasseltui {__version__} — under construction")
    return 0


def _ui_demo(_args: argparse.Namespace) -> int:
    """Launch the Textual UI against the static demo state.

    Imported lazily so that running any of the diagnostic subcommands
    (or just `--version` / `--help`) does not pull Textual into the
    process. Textual's import time is noticeable and the diagnostic
    subcommands share a reason not to pay for it.
    """
    from quasseltui.app.app import QuasselApp
    from quasseltui.app.demo_data import build_demo_state

    QuasselApp(build_demo_state()).run()
    return 0


def _ui(args: argparse.Namespace) -> int:
    """Launch the Textual UI against a live Quassel core.

    Unlike `ui-demo`, this command requires credentials and a reachable
    core. It builds a `QuasselClient` and hands both the client and its
    `ClientState` to `QuasselApp`; the app's `on_mount` hook then starts
    the `ClientBridge` worker which drives the receive loop inside the
    Textual event loop.

    Exit codes: 0 when the user quits cleanly via Ctrl+Q; 1 for bad
    arguments / missing credentials. This subcommand intentionally
    does NOT fan out every protocol error to a unique exit code — the
    user is interacting with the app, so protocol errors surface as
    log warnings and the `SessionEnded` banner in the UI rather than
    as process exit codes. For scripted exit-code semantics use
    `dump-state` or `stream-only`.
    """
    user = args.user or os.environ.get("QUASSEL_USER")
    if not user:
        print("ui: --user or QUASSEL_USER is required", file=sys.stderr)
        return 1
    password = args.password or os.environ.get("QUASSEL_PASSWORD")
    if password is None:
        try:
            password = getpass.getpass(f"Password for {user}@{args.host}: ")
        except (EOFError, KeyboardInterrupt):
            print("\nui: aborted at password prompt", file=sys.stderr)
            return 1
    if not password:
        print("ui: empty password not allowed", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Lazy import for the same reason as `_ui_demo`: keep Textual out
    # of the headless subcommands so `--version` / `--help` stay fast.
    from quasseltui.app.app import QuasselApp

    tls_options = TlsOptions(verify=not args.insecure, cafile=args.cafile)
    client = QuasselClient(
        host=args.host,
        port=args.port,
        user=user,
        password=password,
        tls=not args.no_tls,
        tls_options=tls_options,
        client_version=CLIENT_VERSION,
        build_date=BUILD_DATE,
        connect_timeout=args.connect_timeout,
    )
    # The app owns the client lifecycle from this point — its
    # `on_unmount` closes the connection. We pass the client's own
    # `state` so widgets render from the same store the dispatcher is
    # writing to; a copy here would mean the UI silently lags behind.
    app = QuasselApp(client.state, client=client)
    app.run()
    # Surface fatal exits (pre-session handshake failures) to the
    # shell. Clean quits via Ctrl+Q have return_code=0 or None; early
    # auth/TLS/handshake failures have return_code=1 via the app's
    # `_on_session_ended` fatal branch. `App.exit(message=...)` has
    # already printed the sanitized reason after teardown.
    return app.return_code or 0


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


async def _stream_only(args: argparse.Namespace) -> int:
    """Run the full handshake and stream SignalProxy events to stdout.

    Exit codes mirror `login-only` for the setup phase (0 clean, 1 bad args,
    2 connect fail, 3 init reject, 4 protocol error, 5 TLS downgrade,
    6 unconfigured core, 7 auth rejected). Code 0 also applies when the
    duration elapses and we disconnect cleanly.
    """
    user = args.user or os.environ.get("QUASSEL_USER")
    if not user:
        print("stream-only: --user or QUASSEL_USER is required", file=sys.stderr)
        return 1
    password = args.password or os.environ.get("QUASSEL_PASSWORD")
    if password is None:
        try:
            password = getpass.getpass(f"Password for {user}@{args.host}: ")
        except (EOFError, KeyboardInterrupt):
            print("\nstream-only: aborted at password prompt", file=sys.stderr)
            return 1
    if not password:
        print("stream-only: empty password not allowed", file=sys.stderr)
        return 1

    # Turn on a minimal log handler so WARN from the connection bubbles up
    # without drowning the pretty-printed event stream.
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    tls_options = TlsOptions(verify=not args.insecure, cafile=args.cafile)
    conn = QuasselConnection(
        host=args.host,
        port=args.port,
        user=user,
        password=password,
        tls=not args.no_tls,
        tls_options=tls_options,
        client_version=CLIENT_VERSION,
        build_date=BUILD_DATE,
        connect_timeout=args.connect_timeout,
    )

    max_events = args.max_events
    event_count = 0
    exit_code = 0

    async def run() -> int:
        nonlocal event_count
        async for event in conn.events():
            _print_stream_event(event, verbose=args.verbose)
            if isinstance(event, Disconnected):
                return _stream_disconnect_exit_code(event)
            # SessionReady is the handshake banner, not one of the
            # streamed SignalProxy events that --max-events bounds. With
            # `--max-events=1` the user expects to see exactly one
            # connected-state event, not to exit immediately after the
            # banner prints.
            if isinstance(event, SessionReady):
                continue
            event_count += 1
            if max_events is not None and event_count >= max_events:
                print(f"[max-events={max_events} reached, stopping]")
                return 0
        return 0

    try:
        exit_code = await asyncio.wait_for(run(), timeout=args.duration)
    except TimeoutError:
        print(f"[duration={args.duration}s elapsed, stopping]")
        exit_code = 0
    finally:
        await conn.close()

    return exit_code


def _stream_disconnect_exit_code(event: Disconnected) -> int:
    """Map a terminal Disconnected event to a `login-only`-compatible code."""
    err = event.error
    if isinstance(err, AuthError):
        return 7
    if isinstance(err, TransportError):
        return 2
    reason = event.reason.lower()
    if "tls" in reason and "plaintext" in reason:
        return 5
    if "not configured" in reason:
        return 6
    if "rejected clientinit" in reason:
        return 3
    return 4


def _print_stream_event(event: ProtocolEvent, *, verbose: bool) -> None:
    if isinstance(event, SessionReady):
        _print_session_init(event.session)
        feats = ", ".join(sorted(event.peer_features)) or "(none)"
        print(f"negotiated features: {feats}")
        print("[streaming events…]")
        return
    if isinstance(event, SyncEvent):
        sync = event.message
        suffix = f"  params={sync.params!r}" if verbose else ""
        print(
            f"Sync  {sync.class_name.decode('ascii', 'replace')}::"
            f"{sync.object_name} {sync.slot_name.decode('ascii', 'replace')}"
            f" ({len(sync.params)} params){suffix}"
        )
        return
    if isinstance(event, RpcEvent):
        rpc = event.message
        suffix = f"  params={rpc.params!r}" if verbose else ""
        print(
            f"Rpc   {rpc.signal_name.decode('ascii', 'replace')} ({len(rpc.params)} params){suffix}"
        )
        return
    if isinstance(event, InitDataEvent):
        idm = event.message
        suffix = f"  data={idm.init_data!r}" if verbose else ""
        print(
            f"Init  {idm.class_name.decode('ascii', 'replace')}::"
            f"{idm.object_name} ({len(idm.init_data)} keys){suffix}"
        )
        return
    if isinstance(event, InitRequestEvent):
        req = event.message
        print(f"IReq  {req.class_name.decode('ascii', 'replace')}::{req.object_name}")
        return
    if isinstance(event, HeartBeatEvent):
        print(f"Heart ts={event.message.timestamp.isoformat()}")
        return
    if isinstance(event, Disconnected):
        print(f"-- disconnected: {event.reason}")
        return


async def _dump_state(args: argparse.Namespace) -> int:
    """Run the full `QuasselClient` stack and print a ClientState snapshot.

    Exit codes mirror `login-only` (0 clean, 1 bad args, 2 connect fail,
    3 init reject, 4 protocol error, 5 TLS downgrade, 6 unconfigured core,
    7 auth rejected). We succeed with 0 if the duration elapses normally
    and we were able to print at least a partial snapshot.
    """
    user = args.user or os.environ.get("QUASSEL_USER")
    if not user:
        print("dump-state: --user or QUASSEL_USER is required", file=sys.stderr)
        return 1
    password = args.password or os.environ.get("QUASSEL_PASSWORD")
    if password is None:
        try:
            password = getpass.getpass(f"Password for {user}@{args.host}: ")
        except (EOFError, KeyboardInterrupt):
            print("\ndump-state: aborted at password prompt", file=sys.stderr)
            return 1
    if not password:
        print("dump-state: empty password not allowed", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    tls_options = TlsOptions(verify=not args.insecure, cafile=args.cafile)
    client = QuasselClient(
        host=args.host,
        port=args.port,
        user=user,
        password=password,
        tls=not args.no_tls,
        tls_options=tls_options,
        client_version=CLIENT_VERSION,
        build_date=BUILD_DATE,
        connect_timeout=args.connect_timeout,
    )

    exit_code = 0
    event_counts: dict[str, int] = defaultdict(int)
    last_disconnect: ClientDisconnected | None = None

    async def run() -> None:
        nonlocal last_disconnect
        async for event in client.events():
            event_counts[type(event).__name__] += 1
            if isinstance(event, ClientDisconnected):
                last_disconnect = event
                return
            # Phase 5 "watch for a bit then snapshot" — we don't do per-
            # event printing like stream-only; the whole point is to rely
            # on the sync layer and dump the final state.

    try:
        await asyncio.wait_for(run(), timeout=args.duration)
    except TimeoutError:
        pass
    finally:
        await client.close()

    if last_disconnect is not None:
        exit_code = _dump_state_exit_code(last_disconnect)
        print(f"-- disconnected before duration elapsed: {last_disconnect.reason}")

    _print_state_snapshot(client.state, max_messages=args.max_messages, counts=event_counts)
    return exit_code


def _dump_state_exit_code(event: ClientDisconnected) -> int:
    """Map a `ClientDisconnected` event to a `login-only`-compatible code.

    Intentionally duplicates `_stream_disconnect_exit_code` rather than
    sharing — the event types are different (protocol-layer vs client-
    layer), and trying to generalize over both via structural typing would
    cost more than the few lines saved.
    """
    err = event.error
    if isinstance(err, AuthError):
        return 7
    if isinstance(err, TransportError):
        return 2
    reason = event.reason.lower()
    if "tls" in reason and "plaintext" in reason:
        return 5
    if "not configured" in reason:
        return 6
    if "rejected clientinit" in reason:
        return 3
    return 4


def _print_state_snapshot(
    state: ClientState,
    *,
    max_messages: int,
    counts: dict[str, int],
) -> None:
    """Pretty-print the canonical `ClientState` for dump-state and tests.

    Groups buffers by network, shows per-network connection state + my
    nick, and prints the last `max_messages` IrcMessages per buffer. All
    core-provided strings are sanitized via `_sanitize_terminal` so a
    hostile IRC payload can't inject terminal escape sequences into our
    output.
    """
    print()
    print("=== ClientState snapshot ===")
    feats = ", ".join(sorted(state.peer_features)) or "(none)"
    print(f"peer_features: {_sanitize_terminal(feats)}")
    print(
        f"counts: networks={len(state.networks)}, buffers={len(state.buffers)}, "
        f"identities={len(state.identities)}, messages={state.total_message_count()}"
    )
    if counts:
        print("event_counts:")
        for name in sorted(counts):
            # event names come from our own type() calls, but sanitize
            # defensively anyway so the snapshot printer has one rule.
            print(f"  {_sanitize_terminal(name)}: {counts[name]}")

    if state.identities:
        print("identities:")
        for ident_id in sorted(state.identities, key=int):
            identity = state.identities[ident_id]
            nicks_raw = ", ".join(identity.nicks) if identity.nicks else "(no nicks)"
            name = _sanitize_terminal(identity.identity_name or "(unnamed)")
            nicks = _sanitize_terminal(nicks_raw)
            print(f"  - [{int(ident_id)}] {name} ({nicks})")

    if state.networks:
        print("networks:")
        for network_id in sorted(state.networks, key=int):
            network = state.networks[network_id]
            net_name = _sanitize_terminal(network.network_name or "(unnamed)")
            my_nick = _sanitize_terminal(network.my_nick or "?")
            current_server = _sanitize_terminal(network.current_server or "?")
            print(
                f"  - [{int(network_id)}] {net_name}  "
                f"state={network.connection_state.name} nick={my_nick} "
                f"server={current_server}"
            )
            buffers = [b for b in state.buffers.values() if int(b.network_id) == int(network_id)]
            if not buffers:
                continue
            for buf in sorted(buffers, key=lambda b: (b.type.value, b.name.lower())):
                messages = state.messages.get(buf.buffer_id, [])
                kind = _buffer_type_label(buf.type)
                buf_name = _sanitize_terminal(buf.name or "(unnamed)")
                print(
                    f"      [{kind}] {buf_name} "
                    f"(buffer_id={int(buf.buffer_id)}, {len(messages)} msgs)"
                )
                if messages and max_messages > 0:
                    for msg in messages[-max_messages:]:
                        ts = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                        prefix = _sanitize_terminal(msg.sender_prefixes or " ")
                        sender = _sanitize_terminal(msg.sender)
                        contents = _sanitize_terminal(msg.contents)
                        print(f"          {ts} {prefix}{sender}: {contents}")
    else:
        print("networks: (none)")
