"""Unit tests for the cli module.

These tests focus on the connection-policy decisions the CLI is responsible
for — specifically the fail-closed-on-downgrade path that codex review
flagged as a HIGH-severity issue, plus phase 3's login-only state machine.
Network I/O is mocked at the module boundary so we test the policy without
spinning up sockets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from quasseltui import cli
from quasseltui.protocol.errors import AuthError
from quasseltui.protocol.messages import (
    ClientInitAck,
    ClientInitReject,
    ClientLoginAck,
    SessionInit,
)
from quasseltui.protocol.probe import ConnectionFeature, NegotiatedProtocol, ProtocolType
from quasseltui.protocol.usertypes import (
    BufferId,
    BufferInfo,
    BufferType,
    NetworkId,
)


@pytest.mark.asyncio
async def test_probe_only_aborts_on_tls_downgrade(capsys: pytest.CaptureFixture[str]) -> None:
    """If we offered Encryption and the core's reply did not enable it,
    the CLI must abort BEFORE sending ClientInit. Otherwise an active MITM
    can strip the TLS bit and harvest credentials in a later phase."""
    args = _make_args(no_tls=False, allow_plaintext=False)
    fake_writer = _make_fake_writer()

    with (
        patch.object(
            cli, "open_tcp_connection", new=AsyncMock(return_value=(object(), fake_writer))
        ),
        patch.object(
            cli,
            "probe",
            new=AsyncMock(
                return_value=NegotiatedProtocol(
                    protocol=ProtocolType.DataStream,
                    peer_features=0,
                    connection_features=ConnectionFeature.NONE,
                )
            ),
        ),
        patch.object(cli, "send_client_init", new=AsyncMock()) as send_init,
        patch.object(cli, "recv_handshake_message", new=AsyncMock()) as recv,
        patch.object(cli, "start_tls_on_writer", new=AsyncMock()) as start_tls,
        patch.object(cli, "close_writer", new=AsyncMock()),
    ):
        rc = await cli._probe_only(args)

    assert rc == 5
    # Critical: we must NOT have sent any application data after the
    # downgrade was detected.
    send_init.assert_not_awaited()
    recv.assert_not_awaited()
    start_tls.assert_not_awaited()
    err = capsys.readouterr().err
    assert "downgrade" in err.lower()


@pytest.mark.asyncio
async def test_probe_only_continues_with_allow_plaintext(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The downgrade-abort can be opted out of for trusted-network use
    cases (local cores, lab setups). When --allow-plaintext is set we
    proceed all the way through ClientInitAck without enabling TLS."""
    args = _make_args(no_tls=False, allow_plaintext=True)
    fake_writer = _make_fake_writer()

    fake_ack = ClientInitAck(
        core_features=0,
        feature_list=(),
        configured=True,
        storage_backends=(),
        authenticators=(),
        protocol_version=None,
        raw={},
    )

    with (
        patch.object(
            cli, "open_tcp_connection", new=AsyncMock(return_value=(object(), fake_writer))
        ),
        patch.object(
            cli,
            "probe",
            new=AsyncMock(
                return_value=NegotiatedProtocol(
                    protocol=ProtocolType.DataStream,
                    peer_features=0,
                    connection_features=ConnectionFeature.NONE,
                )
            ),
        ),
        patch.object(cli, "send_client_init", new=AsyncMock()) as send_init,
        patch.object(cli, "recv_handshake_message", new=AsyncMock(return_value=fake_ack)) as recv,
        patch.object(cli, "start_tls_on_writer", new=AsyncMock()) as start_tls,
        patch.object(cli, "close_writer", new=AsyncMock()),
    ):
        rc = await cli._probe_only(args)

    assert rc == 0
    send_init.assert_awaited_once()
    recv.assert_awaited_once()
    start_tls.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_only_uses_tls_when_negotiated() -> None:
    args = _make_args(no_tls=False, allow_plaintext=False)
    fake_writer = _make_fake_writer()

    fake_ack = ClientInitAck(
        core_features=0,
        feature_list=(),
        configured=True,
        storage_backends=(),
        authenticators=(),
        protocol_version=None,
        raw={},
    )

    with (
        patch.object(
            cli, "open_tcp_connection", new=AsyncMock(return_value=(object(), fake_writer))
        ),
        patch.object(
            cli,
            "probe",
            new=AsyncMock(
                return_value=NegotiatedProtocol(
                    protocol=ProtocolType.DataStream,
                    peer_features=0,
                    connection_features=ConnectionFeature.Encryption,
                )
            ),
        ),
        patch.object(cli, "send_client_init", new=AsyncMock()),
        patch.object(cli, "recv_handshake_message", new=AsyncMock(return_value=fake_ack)),
        patch.object(cli, "start_tls_on_writer", new=AsyncMock()) as start_tls,
        patch.object(cli, "close_writer", new=AsyncMock()),
    ):
        rc = await cli._probe_only(args)

    assert rc == 0
    start_tls.assert_awaited_once()


def _make_args(*, no_tls: bool, allow_plaintext: bool) -> Any:
    """Build an argparse.Namespace shaped like the probe-only subcommand.

    `server=None` plus populated host+port keeps `_resolve_connection_args`
    from touching the filesystem — the helper short-circuits when the CLI
    has already supplied an endpoint and no --server was requested.
    """
    import argparse

    return argparse.Namespace(
        server=None,
        host="example.invalid",
        port=4242,
        no_tls=no_tls,
        allow_plaintext=allow_plaintext,
        insecure=False,
        cafile=None,
        connect_timeout=10.0,
    )


def _make_login_args(
    *, no_tls: bool = False, user: str | None = "sean", password: str | None = "hunter2"
) -> Any:
    """Build an argparse.Namespace shaped like the login-only subcommand.

    `--allow-plaintext` does not exist on this subcommand by design — login
    refuses to fall back to plaintext silently because it would leak the
    password.
    """
    import argparse

    return argparse.Namespace(
        server=None,
        host="example.invalid",
        port=4242,
        user=user,
        password=password,
        no_tls=no_tls,
        insecure=False,
        cafile=None,
        connect_timeout=10.0,
    )


def _make_fake_writer() -> Any:
    """Return an object that satisfies whatever cli._probe_only touches on
    the writer (close + drain are reached only via the patched helpers, so
    a bare object is enough)."""
    return object()


def _fake_init_ack(*, configured: bool = True) -> ClientInitAck:
    return ClientInitAck(
        core_features=0,
        feature_list=(),
        configured=configured,
        storage_backends=(),
        authenticators=(),
        protocol_version=None,
        raw={},
    )


def _fake_session_init() -> SessionInit:
    return SessionInit(
        identities=({"identityName": "default", "identityId": 1},),
        network_ids=(NetworkId(1),),
        buffer_infos=(
            BufferInfo(
                buffer_id=BufferId(10),
                network_id=NetworkId(1),
                type=BufferType.Channel,
                group_id=0,
                name="#test",
            ),
        ),
        raw={},
    )


@pytest.mark.asyncio
async def test_login_only_full_handshake_success(capsys: pytest.CaptureFixture[str]) -> None:
    """Happy path: probe → TLS upgrade → ClientInit → ClientInitAck →
    ClientLogin → ClientLoginAck → SessionInit. Returns 0 and prints the
    summary line."""
    args = _make_login_args()
    fake_writer = _make_fake_writer()

    recv_responses = [_fake_init_ack(), ClientLoginAck(raw={}), _fake_session_init()]

    with (
        patch.object(
            cli, "open_tcp_connection", new=AsyncMock(return_value=(object(), fake_writer))
        ),
        patch.object(
            cli,
            "probe",
            new=AsyncMock(
                return_value=NegotiatedProtocol(
                    protocol=ProtocolType.DataStream,
                    peer_features=0,
                    connection_features=ConnectionFeature.Encryption,
                )
            ),
        ),
        patch.object(cli, "send_client_init", new=AsyncMock()) as send_init,
        patch.object(cli, "send_client_login", new=AsyncMock()) as send_login,
        patch.object(cli, "recv_handshake_message", new=AsyncMock(side_effect=recv_responses)),
        patch.object(cli, "start_tls_on_writer", new=AsyncMock()) as start_tls,
        patch.object(cli, "close_writer", new=AsyncMock()),
    ):
        rc = await cli._login_only(args)

    assert rc == 0
    send_init.assert_awaited_once()
    send_login.assert_awaited_once()
    start_tls.assert_awaited_once()
    out = capsys.readouterr().out
    assert "1 identities" in out
    assert "1 networks" in out
    assert "1 buffers" in out
    assert "#test" in out


@pytest.mark.asyncio
async def test_login_only_rejects_tls_downgrade_with_no_escape_hatch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The login-only command MUST not have a `--allow-plaintext` flag.
    Even if the user really wants plaintext, they have to opt in via
    `--no-tls` *before* the probe — silent downgrade is never an option
    because the password would leak."""
    args = _make_login_args(no_tls=False)
    fake_writer = _make_fake_writer()

    with (
        patch.object(
            cli, "open_tcp_connection", new=AsyncMock(return_value=(object(), fake_writer))
        ),
        patch.object(
            cli,
            "probe",
            new=AsyncMock(
                return_value=NegotiatedProtocol(
                    protocol=ProtocolType.DataStream,
                    peer_features=0,
                    connection_features=ConnectionFeature.NONE,
                )
            ),
        ),
        patch.object(cli, "send_client_init", new=AsyncMock()) as send_init,
        patch.object(cli, "send_client_login", new=AsyncMock()) as send_login,
        patch.object(cli, "recv_handshake_message", new=AsyncMock()) as recv,
        patch.object(cli, "start_tls_on_writer", new=AsyncMock()) as start_tls,
        patch.object(cli, "close_writer", new=AsyncMock()),
    ):
        rc = await cli._login_only(args)

    assert rc == 5
    # Critical: nothing should have been sent — not even ClientInit.
    send_init.assert_not_awaited()
    send_login.assert_not_awaited()
    recv.assert_not_awaited()
    start_tls.assert_not_awaited()
    err = capsys.readouterr().err
    assert "downgrade" in err.lower()


@pytest.mark.asyncio
async def test_login_only_unconfigured_core_aborts(capsys: pytest.CaptureFixture[str]) -> None:
    """If the core's ClientInitAck has Configured=False we abort BEFORE
    sending credentials — we don't ship the CoreSetupData wizard."""
    args = _make_login_args()
    fake_writer = _make_fake_writer()

    recv_responses = [_fake_init_ack(configured=False)]

    with (
        patch.object(
            cli, "open_tcp_connection", new=AsyncMock(return_value=(object(), fake_writer))
        ),
        patch.object(
            cli,
            "probe",
            new=AsyncMock(
                return_value=NegotiatedProtocol(
                    protocol=ProtocolType.DataStream,
                    peer_features=0,
                    connection_features=ConnectionFeature.Encryption,
                )
            ),
        ),
        patch.object(cli, "send_client_init", new=AsyncMock()),
        patch.object(cli, "send_client_login", new=AsyncMock()) as send_login,
        patch.object(cli, "recv_handshake_message", new=AsyncMock(side_effect=recv_responses)),
        patch.object(cli, "start_tls_on_writer", new=AsyncMock()),
        patch.object(cli, "close_writer", new=AsyncMock()),
    ):
        rc = await cli._login_only(args)

    assert rc == 6
    send_login.assert_not_awaited()
    assert "not configured" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_login_only_init_reject_returns_3(capsys: pytest.CaptureFixture[str]) -> None:
    args = _make_login_args()
    fake_writer = _make_fake_writer()

    init_reject = ClientInitReject(error_string="too old", raw={})

    with (
        patch.object(
            cli, "open_tcp_connection", new=AsyncMock(return_value=(object(), fake_writer))
        ),
        patch.object(
            cli,
            "probe",
            new=AsyncMock(
                return_value=NegotiatedProtocol(
                    protocol=ProtocolType.DataStream,
                    peer_features=0,
                    connection_features=ConnectionFeature.Encryption,
                )
            ),
        ),
        patch.object(cli, "send_client_init", new=AsyncMock()),
        patch.object(cli, "send_client_login", new=AsyncMock()) as send_login,
        patch.object(cli, "recv_handshake_message", new=AsyncMock(side_effect=[init_reject])),
        patch.object(cli, "start_tls_on_writer", new=AsyncMock()),
        patch.object(cli, "close_writer", new=AsyncMock()),
    ):
        rc = await cli._login_only(args)

    assert rc == 3
    send_login.assert_not_awaited()
    assert "REJECTED" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_login_only_auth_error_returns_7(capsys: pytest.CaptureFixture[str]) -> None:
    """ClientLoginReject is converted to AuthError by the parser. The CLI
    must catch it and exit with code 7 — distinct from the protocol-level
    code 4 so reconnect supervisors can tell them apart."""
    args = _make_login_args()
    fake_writer = _make_fake_writer()

    recv_responses = [_fake_init_ack(), AuthError("bad password")]

    with (
        patch.object(
            cli, "open_tcp_connection", new=AsyncMock(return_value=(object(), fake_writer))
        ),
        patch.object(
            cli,
            "probe",
            new=AsyncMock(
                return_value=NegotiatedProtocol(
                    protocol=ProtocolType.DataStream,
                    peer_features=0,
                    connection_features=ConnectionFeature.Encryption,
                )
            ),
        ),
        patch.object(cli, "send_client_init", new=AsyncMock()),
        patch.object(cli, "send_client_login", new=AsyncMock()),
        patch.object(cli, "recv_handshake_message", new=AsyncMock(side_effect=recv_responses)),
        patch.object(cli, "start_tls_on_writer", new=AsyncMock()),
        patch.object(cli, "close_writer", new=AsyncMock()),
    ):
        rc = await cli._login_only(args)

    assert rc == 7
    err = capsys.readouterr().err
    assert "bad password" in err


@pytest.mark.asyncio
async def test_login_only_missing_user_returns_1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --user, no QUASSEL_USER → exit 1 before opening any sockets."""
    monkeypatch.delenv("QUASSEL_USER", raising=False)
    args = _make_login_args(user=None)

    with patch.object(cli, "open_tcp_connection", new=AsyncMock()) as open_conn:
        rc = await cli._login_only(args)

    assert rc == 1
    open_conn.assert_not_awaited()
    assert "QUASSEL_USER" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# dump-state (phase 5). Exercises the `QuasselClient.events()` transform
# via a fake client whose events() yields a canned sequence, then asserts
# on the printed snapshot. We don't hit the protocol layer at all — the
# client-layer code paths are what matter here.
# ---------------------------------------------------------------------------


def _make_dump_args(
    *,
    user: str | None = "sean",
    password: str | None = "hunter2",
    duration: float = 0.1,
    max_messages: int = 3,
) -> Any:
    import argparse

    return argparse.Namespace(
        server=None,
        host="example.invalid",
        port=4242,
        user=user,
        password=password,
        no_tls=False,
        insecure=False,
        cafile=None,
        connect_timeout=10.0,
        duration=duration,
        max_messages=max_messages,
    )


class _FakeClientForDumpState:
    """Stand-in for `QuasselClient` used by the dump-state test.

    Pre-populates its `state` with a small but realistic world and yields
    a single terminal `ClientDisconnected` so `_dump_state`'s run loop
    finishes immediately. Uses real `ClientState` + event dataclasses so
    the snapshot printer doesn't need a parallel stub hierarchy.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        from datetime import UTC, datetime

        from quasseltui.client.events import Disconnected as _Disconnected
        from quasseltui.client.state import ClientState as _ClientState
        from quasseltui.protocol.enums import MessageFlag, MessageType
        from quasseltui.protocol.usertypes import (
            BufferId as _BufferId,
        )
        from quasseltui.protocol.usertypes import (
            BufferInfo as _BufferInfo,
        )
        from quasseltui.protocol.usertypes import (
            BufferType as _BufferType,
        )
        from quasseltui.protocol.usertypes import (
            IdentityId as _IdentityId,
        )
        from quasseltui.protocol.usertypes import (
            MsgId as _MsgId,
        )
        from quasseltui.protocol.usertypes import (
            NetworkId as _NetworkId,
        )
        from quasseltui.sync.events import IrcMessage
        from quasseltui.sync.identity import Identity
        from quasseltui.sync.network import Network, NetworkConnectionState

        self.state = _ClientState()
        network = Network(object_name="1")
        network.network_name = "freenode"
        network.my_nick = "seanr"
        network.connection_state = NetworkConnectionState.Initialized
        self.state.networks[_NetworkId(1)] = network
        self.state.buffers[_BufferId(10)] = _BufferInfo(
            buffer_id=_BufferId(10),
            network_id=_NetworkId(1),
            type=_BufferType.Channel,
            group_id=0,
            name="#python",
        )
        msg = IrcMessage(
            msg_id=_MsgId(1),
            buffer_id=_BufferId(10),
            network_id=_NetworkId(1),
            timestamp=datetime(2026, 4, 14, 12, 0, tzinfo=UTC),
            type=MessageType.Plain,
            flags=MessageFlag.NONE,
            sender="alice",
            sender_prefixes="@",
            contents="hello from alice",
        )
        self.state.messages[_BufferId(10)] = [msg]
        identity = Identity(object_name="1")
        identity.identity_name = "default"
        identity.nicks = ["sean", "sean_"]
        self.state.identities[_IdentityId(1)] = identity

        self._terminal = _Disconnected(reason="clean shutdown", error=None)

    async def events(self) -> Any:
        yield self._terminal

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_dump_state_prints_snapshot(capsys: pytest.CaptureFixture[str]) -> None:
    """The dump-state handler must print every section the sync layer
    populated into `ClientState` — networks with their connection state,
    per-network buffer listings, and recent messages."""
    args = _make_dump_args()

    with patch.object(cli, "QuasselClient", new=_FakeClientForDumpState):
        rc = await cli._dump_state(args)

    # Clean shutdown with no error → exit 4 (our disconnect reason lacks
    # any of the recognized tokens, so it's classified as a generic
    # protocol error). The important part of the test is the snapshot
    # printing, not the exit code.
    out = capsys.readouterr().out
    assert "ClientState snapshot" in out
    assert "freenode" in out
    assert "#python" in out
    assert "alice" in out
    assert "hello from alice" in out
    assert "default" in out
    # And the handler returned an integer exit code.
    assert isinstance(rc, int)


@pytest.mark.asyncio
async def test_dump_state_missing_user_returns_1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QUASSEL_USER", raising=False)
    args = _make_dump_args(user=None)

    with patch.object(cli, "QuasselClient") as client_cls:
        rc = await cli._dump_state(args)

    assert rc == 1
    client_cls.assert_not_called()
    assert "QUASSEL_USER" in capsys.readouterr().err


def test_sanitize_terminal_escapes_control_chars() -> None:
    """Regression for codex review finding: untrusted IRC strings flowed
    straight into `print()` from `dump-state`, allowing a hostile peer
    to inject ANSI escape sequences and rewrite the operator's terminal.
    The sanitizer must escape any C0/C1 control char.

    The implementation lives in `quasseltui.util.text` now (phase 6's
    second codex review pulled it out of `cli.py`); we re-import through
    `cli._sanitize_terminal` here to verify the cli module still re-
    exposes the same function object.
    """
    # Plain text and unicode pass through unchanged.
    assert cli._sanitize_terminal("hello world") == "hello world"
    assert cli._sanitize_terminal("résumé") == "résumé"
    assert cli._sanitize_terminal("#python") == "#python"

    # ESC (0x1b), the start of every ANSI escape sequence.
    raw_red = "\x1b[31mRED"
    cleaned = cli._sanitize_terminal(raw_red)
    assert "\x1b" not in cleaned
    assert "\\x1b" in cleaned

    # Newline / carriage return / tab — single-line snapshot output.
    assert "\n" not in cli._sanitize_terminal("line1\nline2")
    assert "\r" not in cli._sanitize_terminal("over\rwrite")
    assert "\t" not in cli._sanitize_terminal("col1\tcol2")

    # NUL, BEL, BS — the usual terminal-spoofing primitives.
    assert "\x00" not in cli._sanitize_terminal("a\x00b")
    assert "\x07" not in cli._sanitize_terminal("a\x07b")
    assert "\x08" not in cli._sanitize_terminal("a\x08b")

    # C1 control range (0x80-0x9f) — some terminals interpret these as
    # CSI prefixes even though they look like high-bit Latin-1 bytes.
    assert "\x9b" not in cli._sanitize_terminal("a\x9bb")


@pytest.mark.asyncio
async def test_dump_state_sanitizes_malicious_irc_strings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: a network whose name contains an ANSI escape MUST
    not flow that escape into the printed snapshot. Pins the integration
    of `_sanitize_terminal` into the snapshot printer."""
    from datetime import UTC, datetime

    from quasseltui.client.events import Disconnected as _Disconnected
    from quasseltui.client.state import ClientState as _ClientState
    from quasseltui.protocol.enums import MessageFlag, MessageType
    from quasseltui.protocol.usertypes import (
        BufferId as _BufferId,
    )
    from quasseltui.protocol.usertypes import (
        BufferInfo as _BufferInfo,
    )
    from quasseltui.protocol.usertypes import (
        BufferType as _BufferType,
    )
    from quasseltui.protocol.usertypes import (
        MsgId as _MsgId,
    )
    from quasseltui.protocol.usertypes import (
        NetworkId as _NetworkId,
    )
    from quasseltui.sync.events import IrcMessage
    from quasseltui.sync.network import Network, NetworkConnectionState

    class _MaliciousFakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.state = _ClientState()
            net = Network(object_name="1")
            net.network_name = "evil\x1b[31mNET"
            net.my_nick = "ghost\x07"
            net.connection_state = NetworkConnectionState.Initialized
            self.state.networks[_NetworkId(1)] = net
            self.state.buffers[_BufferId(10)] = _BufferInfo(
                buffer_id=_BufferId(10),
                network_id=_NetworkId(1),
                type=_BufferType.Channel,
                group_id=0,
                name="#fake\x1b]0;OWNED\x07",
            )
            self.state.messages[_BufferId(10)] = [
                IrcMessage(
                    msg_id=_MsgId(1),
                    buffer_id=_BufferId(10),
                    network_id=_NetworkId(1),
                    timestamp=datetime(2026, 4, 14, 12, 0, tzinfo=UTC),
                    type=MessageType.Plain,
                    flags=MessageFlag.NONE,
                    sender="badnick\x08\x08\x08sneaky",
                    sender_prefixes="@",
                    contents="hi\x1b[2J\x1b[H",
                )
            ]
            self._terminal = _Disconnected(reason="clean shutdown", error=None)

        async def events(self) -> Any:
            yield self._terminal

        async def close(self) -> None:
            return None

    args = _make_dump_args()
    with patch.object(cli, "QuasselClient", new=_MaliciousFakeClient):
        await cli._dump_state(args)

    out = capsys.readouterr().out
    # No raw escape sequences anywhere in the output. The presence of
    # any of these would indicate the terminal-injection regression
    # has been re-introduced.
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "\x08" not in out
    # The escaped form should appear in place of each control char.
    assert "\\x1b" in out
    assert "\\x07" in out
    assert "\\x08" in out


# ---------------------------------------------------------------------------
# Config-file integration (phase 11). The config layer itself is tested in
# tests/unit/test_config.py; these tests pin the *integration* — that the
# argv normalizer routes bare / server-shortcut invocations to `ui`, and
# that the resolver merges config values into `args` with the right
# precedence when --server is given.
# ---------------------------------------------------------------------------


def test_normalize_argv_bare_without_default_server_leaves_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.config, "load", lambda: None)
    assert cli._normalize_argv([]) == []


def test_normalize_argv_bare_with_default_server_injects_ui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = cli.config.Config(
        path=tmp_path / "config.ini",
        default_server="home",
        servers={"home": cli.config.ServerConfig(name="home")},
    )
    monkeypatch.setattr(cli.config, "load", lambda: fake)
    assert cli._normalize_argv([]) == ["ui"]


def test_normalize_argv_server_shortcut() -> None:
    assert cli._normalize_argv(["home"]) == ["ui", "--server", "home"]
    assert cli._normalize_argv(["home", "--insecure"]) == [
        "ui",
        "--server",
        "home",
        "--insecure",
    ]


def test_normalize_argv_known_subcommand_unchanged() -> None:
    for cmd in ("probe-only", "login-only", "stream-only", "dump-state", "ui-demo", "ui"):
        assert cli._normalize_argv([cmd, "--host", "x"]) == [cmd, "--host", "x"]


def test_normalize_argv_leading_flag_unchanged() -> None:
    """`--version` and friends must pass through to argparse untouched."""
    assert cli._normalize_argv(["--version"]) == ["--version"]
    assert cli._normalize_argv(["--help"]) == ["--help"]


def test_resolve_connection_args_short_circuits_when_cli_complete() -> None:
    """If the CLI already supplies both host and port and no --server is
    set, the resolver MUST NOT load the config file. This is what lets
    the existing mocked-namespace tests run without a config fixture."""
    import argparse

    args = argparse.Namespace(
        server=None,
        host="cli.example",
        port=4242,
        user=None,
        password=None,
        no_tls=False,
        insecure=False,
        cafile=None,
        connect_timeout=None,
    )
    called = False

    def _boom() -> None:
        nonlocal called
        called = True
        raise AssertionError("config.load must not be called on the short-circuit path")

    # Patch at the module level — if the resolver reaches config.load it blows up.
    with patch.object(cli.config, "load", side_effect=_boom):
        ok = cli._resolve_connection_args(args, "test")

    assert ok is True
    assert called is False
    # The 10.0 fallback must still be applied even on the short-circuit path.
    assert args.connect_timeout == 10.0


def test_resolve_connection_args_fills_from_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import argparse

    fake = cli.config.Config(
        path=tmp_path / "config.ini",
        default_server=None,
        servers={
            "home": cli.config.ServerConfig(
                name="home",
                host="irc.example.com",
                port=4242,
                user="sean",
                password="hunter2",
                tls=False,
                insecure=True,
                cafile="/etc/ssl/custom.pem",
                connect_timeout=15.0,
            )
        },
    )
    monkeypatch.setattr(cli.config, "load", lambda: fake)

    args = argparse.Namespace(
        server="home",
        host=None,
        port=None,
        user=None,
        password=None,
        no_tls=False,
        insecure=False,
        cafile=None,
        connect_timeout=None,
    )
    assert cli._resolve_connection_args(args, "ui") is True
    assert args.host == "irc.example.com"
    assert args.port == 4242
    assert args.user == "sean"
    assert args.password == "hunter2"
    assert args.no_tls is True
    assert args.insecure is True
    assert args.cafile == "/etc/ssl/custom.pem"
    assert args.connect_timeout == 15.0


def test_resolve_connection_args_cli_overrides_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit CLI flags always win over config values."""
    import argparse

    fake = cli.config.Config(
        path=tmp_path / "config.ini",
        default_server=None,
        servers={
            "home": cli.config.ServerConfig(
                name="home", host="config.example", port=9999, user="configuser"
            )
        },
    )
    monkeypatch.setattr(cli.config, "load", lambda: fake)

    args = argparse.Namespace(
        server="home",
        host="cli.example",  # overrides config
        port=1234,  # overrides config
        user="cliuser",  # overrides config
        password=None,
        no_tls=False,
        insecure=False,
        cafile=None,
        connect_timeout=None,
    )
    assert cli._resolve_connection_args(args, "ui") is True
    assert args.host == "cli.example"
    assert args.port == 1234
    assert args.user == "cliuser"


def test_resolve_connection_args_unknown_server_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse

    fake = cli.config.Config(
        path=tmp_path / "config.ini",
        default_server=None,
        servers={"home": cli.config.ServerConfig(name="home")},
    )
    monkeypatch.setattr(cli.config, "load", lambda: fake)

    args = argparse.Namespace(
        server="ghost",
        host=None,
        port=None,
        user=None,
        password=None,
        no_tls=False,
        insecure=False,
        cafile=None,
        connect_timeout=None,
    )
    assert cli._resolve_connection_args(args, "ui") is False
    err = capsys.readouterr().err
    assert "ghost" in err


def test_resolve_connection_args_missing_everything_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No CLI host/port, no config file, no --server → explicit error."""
    import argparse

    monkeypatch.setattr(cli.config, "load", lambda: None)
    args = argparse.Namespace(
        server=None,
        host=None,
        port=None,
        user=None,
        password=None,
        no_tls=False,
        insecure=False,
        cafile=None,
        connect_timeout=None,
    )
    assert cli._resolve_connection_args(args, "ui") is False
    err = capsys.readouterr().err
    assert "host is required" in err


def test_resolve_connection_args_surfaces_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed config file must produce a user-facing error, not a
    silent fall-through to the "host required" generic message."""
    import argparse

    def _boom() -> cli.config.Config:
        raise cli.config.ConfigError("boom")

    monkeypatch.setattr(cli.config, "load", _boom)
    args = argparse.Namespace(
        server="home",
        host=None,
        port=None,
        user=None,
        password=None,
        no_tls=False,
        insecure=False,
        cafile=None,
        connect_timeout=None,
    )
    assert cli._resolve_connection_args(args, "ui") is False
    assert "boom" in capsys.readouterr().err


def test_main_with_no_subcommand_prints_help_and_exits_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for phase 7 follow-up: running `quasseltui` with no
    subcommand used to print a placeholder "under construction" banner
    and exit 0, which left new users unable to discover the six
    available subcommands. The fix routes through `parser.exit(2, ...)`
    so the bare-invocation path looks identical to every other argparse
    error (unknown flag, bad type, etc.) — it raises `SystemExit(2)`
    after printing the full help to stderr. This test pins that user-
    visible contract so a future refactor cannot silently drop it.

    With config-file support added, bare invocation routes to `ui` when
    a `default_server` is set. So this test force-loads a no-config
    state via monkeypatch to pin the original fall-through behavior for
    the "no config, no default" case.
    """
    monkeypatch.setattr(cli.config, "load", lambda: None)

    with pytest.raises(SystemExit) as exc_info:
        cli.main([])

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    # Full help page, not just argparse's one-line usage error.
    assert "usage: quasseltui" in err
    # Every subcommand advertised in `build_parser` must appear so the
    # user immediately sees what they can actually run next.
    for subcommand in ("probe-only", "login-only", "stream-only", "dump-state", "ui-demo", "ui"):
        assert subcommand in err
