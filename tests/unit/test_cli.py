"""Unit tests for the cli module.

These tests focus on the connection-policy decisions the CLI is responsible
for — specifically the fail-closed-on-downgrade path that codex review
flagged as a HIGH-severity issue. Network I/O is mocked at the module
boundary so we test the policy without spinning up sockets.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from quasseltui import cli
from quasseltui.protocol.messages import ClientInitAck
from quasseltui.protocol.probe import ConnectionFeature, NegotiatedProtocol, ProtocolType


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


def _make_fake_writer() -> Any:
    """Return an object that satisfies whatever cli._probe_only touches on
    the writer (close + drain are reached only via the patched helpers, so
    a bare object is enough)."""
    return object()
