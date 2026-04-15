"""Unit tests for the cli module.

These tests focus on the connection-policy decisions the CLI is responsible
for — specifically the fail-closed-on-downgrade path that codex review
flagged as a HIGH-severity issue, plus phase 3's login-only state machine.
Network I/O is mocked at the module boundary so we test the policy without
spinning up sockets.
"""

from __future__ import annotations

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
    """Build an argparse.Namespace shaped like the probe-only subcommand."""
    import argparse

    return argparse.Namespace(
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
