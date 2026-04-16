"""Tests for `quasseltui.config`.

These exercise the INI loader — default paths, happy-path parsing, error
surfacing for malformed files, and the `resolve_server` helper. All test
config files are created inside `tmp_path` so they never touch the
developer's real `~/.config/quasseltui/config.ini`.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from quasseltui import config


def _write(path: Path, body: str) -> Path:
    """Write `body` to `path` after `textwrap.dedent` — lets the tests
    use natural triple-quoted strings with Python-indent without the
    leading whitespace confusing configparser (which treats indented
    lines as continuations of the previous value)."""
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_load_missing_file_returns_none(tmp_path: Path) -> None:
    """A missing config file is not an error — it's the common case on
    first run and the signal to fall back to CLI args entirely."""
    missing = tmp_path / "config.ini"
    assert config.load(missing) is None


def test_load_parses_full_server(tmp_path: Path) -> None:
    body = """
    [quasseltui]
    default_server = home

    [server:home]
    host = irc.example.com
    port = 4242
    user = sean
    password = hunter2
    tls = true
    insecure = false
    cafile = /etc/ssl/certs/quassel.pem
    connect_timeout = 15
    """
    cfg = config.load(_write(tmp_path / "config.ini", body))
    assert cfg is not None
    assert cfg.default_server == "home"
    assert set(cfg.servers) == {"home"}
    home = cfg.servers["home"]
    assert home.host == "irc.example.com"
    assert home.port == 4242
    assert home.user == "sean"
    assert home.password == "hunter2"
    assert home.tls is True
    assert home.insecure is False
    assert home.cafile == "/etc/ssl/certs/quassel.pem"
    assert home.connect_timeout == 15.0


def test_load_multiple_servers(tmp_path: Path) -> None:
    """Multiple [server:*] sections coexist and the default_server picker
    chooses between them."""
    body = """
    [quasseltui]
    default_server = work

    [server:home]
    host = irc.home.example
    port = 4242

    [server:work]
    host = irc.work.example
    port = 4242
    user = alice
    """
    cfg = config.load(_write(tmp_path / "config.ini", body))
    assert cfg is not None
    assert set(cfg.servers) == {"home", "work"}
    assert cfg.resolve_server(None) is cfg.servers["work"]
    assert cfg.resolve_server("home") is cfg.servers["home"]
    assert cfg.resolve_server("work") is cfg.servers["work"]
    assert cfg.resolve_server("nope") is None


def test_load_without_main_section(tmp_path: Path) -> None:
    """`[quasseltui]` is optional; a file with only server sections and
    no default_server is valid but `resolve_server(None)` returns None."""
    body = """
    [server:home]
    host = irc.example.com
    port = 4242
    """
    cfg = config.load(_write(tmp_path / "config.ini", body))
    assert cfg is not None
    assert cfg.default_server is None
    assert cfg.resolve_server(None) is None
    assert cfg.resolve_server("home") is not None


def test_load_rejects_unknown_section(tmp_path: Path) -> None:
    """Unknown top-level sections are flagged so typos like [servers:foo]
    (note the plural) don't silently do nothing."""
    body = """
    [servers:home]
    host = irc.example.com
    """
    with pytest.raises(config.ConfigError, match="unknown section"):
        config.load(_write(tmp_path / "config.ini", body))


def test_load_rejects_empty_server_name(tmp_path: Path) -> None:
    body = """
    [server:]
    host = irc.example.com
    """
    with pytest.raises(config.ConfigError, match="empty server name"):
        config.load(_write(tmp_path / "config.ini", body))


def test_load_rejects_unknown_server_key(tmp_path: Path) -> None:
    """Typos in server-section keys must surface, not be silently ignored."""
    body = """
    [server:home]
    host = irc.example.com
    passwrod = oops
    """
    with pytest.raises(config.ConfigError, match="unknown setting"):
        config.load(_write(tmp_path / "config.ini", body))


def test_load_rejects_bad_port(tmp_path: Path) -> None:
    body = """
    [server:home]
    host = irc.example.com
    port = notanumber
    """
    with pytest.raises(config.ConfigError, match="port"):
        config.load(_write(tmp_path / "config.ini", body))


def test_load_rejects_bad_bool(tmp_path: Path) -> None:
    body = """
    [server:home]
    host = irc.example.com
    tls = maybe
    """
    with pytest.raises(config.ConfigError, match="tls"):
        config.load(_write(tmp_path / "config.ini", body))


def test_load_rejects_dangling_default_server(tmp_path: Path) -> None:
    """If default_server names a section that doesn't exist, fail loud —
    otherwise a bare `quasseltui` would mysteriously act as if there were
    no default."""
    body = """
    [quasseltui]
    default_server = ghost

    [server:home]
    host = irc.example.com
    port = 4242
    """
    with pytest.raises(config.ConfigError, match="default_server"):
        config.load(_write(tmp_path / "config.ini", body))


def test_load_empty_password_treated_as_unset(tmp_path: Path) -> None:
    """`password =` with nothing after it should fall through to the
    interactive prompt, not produce an empty-password login attempt."""
    body = """
    [server:home]
    host = irc.example.com
    port = 4242
    password =
    """
    cfg = config.load(_write(tmp_path / "config.ini", body))
    assert cfg is not None
    assert cfg.servers["home"].password is None


def test_default_config_path_honors_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.default_config_path() == tmp_path / "quasseltui" / "config.ini"


def test_default_config_path_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.default_config_path() == tmp_path / ".config" / "quasseltui" / "config.ini"
