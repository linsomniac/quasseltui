"""User-config loader for quasseltui.

Reads an INI file so that the host/port/credentials/TLS knobs do not
need to reappear on every command-line invocation. The default search
location honors XDG:

    $XDG_CONFIG_HOME/quasseltui/config.ini
    ~/.config/quasseltui/config.ini            (if XDG_CONFIG_HOME is unset)

Multiple named servers can be defined and one of them marked as the
default, so the user can run either:

    quasseltui                  # connects to [server:$default_server]
    quasseltui <SERVER>         # connects to [server:<SERVER>]

Example file:

    [quasseltui]
    default_server = home

    [server:home]
    host = irc.example.com
    port = 4242
    user = sean
    password = hunter2
    # tls = true            (default)
    # insecure = false      (skip TLS cert verification; self-signed cores)
    # cafile = /etc/ssl/certs/quassel.pem
    # connect_timeout = 10

    [server:work]
    host = irc.work.example
    port = 4242
    user = sreifschneider

Any setting is optional; anything missing falls back to the CLI flag
(and then, for `user`/`password`, to QUASSEL_USER/QUASSEL_PASSWORD or an
interactive prompt, same as before). Storing the password in this file
is allowed by deliberate policy — ensure the file is mode 0600 so other
users on the system cannot read it. We do *not* warn at load time
because that would be noise on every run for the common case of a
correctly-permissioned personal config.

AIDEV-NOTE: The INI format was chosen over TOML at the user's request
so that the file is comfortable to hand-edit without worrying about
TOML's stricter quoting rules around paths and special characters in
passwords.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

_MAIN_SECTION = "quasseltui"
_SERVER_PREFIX = "server:"

# Whitelist of keys each [server:NAME] section may set. Unknown keys are
# reported as errors so typos ("passwrod", "catfile") surface immediately
# rather than silently having no effect.
_ALLOWED_SERVER_KEYS = frozenset(
    {
        "host",
        "port",
        "user",
        "password",
        "tls",
        "insecure",
        "cafile",
        "connect_timeout",
    }
)


class ConfigError(Exception):
    """Raised when the config file exists but cannot be parsed or is invalid."""


@dataclass
class ServerConfig:
    """One named server's connection settings.

    Every field is optional. Fields left as `None` fall back to the CLI
    flag (or env var / prompt, for `user` / `password`).
    """

    name: str
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    tls: bool | None = None
    insecure: bool | None = None
    cafile: str | None = None
    connect_timeout: float | None = None


@dataclass
class Config:
    """The parsed contents of the config file."""

    path: Path
    default_server: str | None = None
    servers: dict[str, ServerConfig] = field(default_factory=dict)

    def resolve_server(self, name: str | None) -> ServerConfig | None:
        """Return the named server, or the default if `name` is None.

        Returns None if neither a name nor a default is available, or if
        the requested name doesn't match any section.
        """
        target = name if name is not None else self.default_server
        if target is None:
            return None
        return self.servers.get(target)


def default_config_path() -> Path:
    """Return the XDG-honoring default path, regardless of whether it exists."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "quasseltui" / "config.ini"


def load(path: Path | None = None) -> Config | None:
    """Read and parse the config file.

    Returns None if the file does not exist — a missing config is not an
    error, just a signal that the caller must fall back to CLI args or
    env vars for everything. Raises `ConfigError` if the file exists but
    is malformed; callers should surface the message to stderr and exit
    non-zero rather than silently ignoring it.
    """
    if path is None:
        path = default_config_path()
    if not path.exists():
        return None

    parser = configparser.ConfigParser()
    try:
        with path.open(encoding="utf-8") as fh:
            parser.read_file(fh)
    except (configparser.Error, OSError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    default_server: str | None = None
    if parser.has_section(_MAIN_SECTION):
        value = parser.get(_MAIN_SECTION, "default_server", fallback="").strip()
        default_server = value or None

    servers: dict[str, ServerConfig] = {}
    for section in parser.sections():
        if section == _MAIN_SECTION:
            continue
        if not section.startswith(_SERVER_PREFIX):
            raise ConfigError(
                f"{path}: unknown section [{section}] (expected [quasseltui] or [server:NAME])"
            )
        name = section[len(_SERVER_PREFIX) :].strip()
        if not name:
            raise ConfigError(f"{path}: empty server name in section [{section}]")
        servers[name] = _parse_server(path, name, parser[section])

    if default_server is not None and default_server not in servers:
        raise ConfigError(
            f"{path}: default_server = {default_server!r} has no matching "
            f"[server:{default_server}] section"
        )

    return Config(path=path, default_server=default_server, servers=servers)


def _parse_server(path: Path, name: str, section: configparser.SectionProxy) -> ServerConfig:
    """Turn one [server:NAME] section into a validated ServerConfig."""
    for key in section:
        if key not in _ALLOWED_SERVER_KEYS:
            raise ConfigError(
                f"{path}: [server:{name}] unknown setting {key!r} "
                f"(allowed: {', '.join(sorted(_ALLOWED_SERVER_KEYS))})"
            )

    def _str(key: str) -> str | None:
        value = section.get(key, fallback="").strip()
        return value or None

    def _int(key: str) -> int | None:
        raw = section.get(key, fallback="").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"{path}: [server:{name}] {key}: {exc}") from exc

    def _float(key: str) -> float | None:
        raw = section.get(key, fallback="").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"{path}: [server:{name}] {key}: {exc}") from exc

    def _bool(key: str) -> bool | None:
        if not section.get(key, fallback="").strip():
            return None
        try:
            return section.getboolean(key)
        except ValueError as exc:
            raise ConfigError(f"{path}: [server:{name}] {key}: {exc}") from exc

    # Passwords are read verbatim (no strip) so leading/trailing spaces
    # survive, but an empty value is still treated as "not set" rather
    # than an intentional empty password — those would fail the login
    # anyway, and treating empty as unset means the interactive prompt
    # still fires when the field is present but blank.
    raw_password = section.get("password", "")
    password = raw_password if raw_password else None

    return ServerConfig(
        name=name,
        host=_str("host"),
        port=_int("port"),
        user=_str("user"),
        password=password,
        tls=_bool("tls"),
        insecure=_bool("insecure"),
        cafile=_str("cafile"),
        connect_timeout=_float("connect_timeout"),
    )


__all__ = [
    "Config",
    "ConfigError",
    "ServerConfig",
    "default_config_path",
    "load",
]
