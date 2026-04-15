"""Typed dataclasses for the handshake-state messages.

The handshake state of a Quassel connection consists of a small fixed set of
QVariantMap-shaped messages exchanged in a fixed order:

    Client → ClientInit            ──┐
    Core   → ClientInitAck           │  setup
                  or ClientInitReject │
    Client → ClientLogin             │  auth
    Core   → ClientLoginAck            │
                  or ClientLoginReject │
    Core   → SessionInit             ──┘  setup → connected

Each message is a flat key→value map on the wire. We model each as a
dataclass plus a from-dict / to-dict converter so the rest of the protocol
layer never has to reach into raw QVariantMaps. Phase 2 covers ClientInit
and ClientInitAck/Reject only — Phase 3 will add the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from quasseltui.protocol.errors import HandshakeError

CLIENT_INIT = "ClientInit"
CLIENT_INIT_ACK = "ClientInitAck"
CLIENT_INIT_REJECT = "ClientInitReject"


def _require_int(data: dict[str, Any], key: str) -> int:
    """Pull a required int field out of a handshake map, or raise.

    Quassel sends ints as `quint32` etc. which decode to Python `int`. Any
    other type means the peer is broken or hostile, and we want that to
    surface as `HandshakeError` rather than a stray `TypeError` escaping
    past the connection state machine's `except QuasselError` handler.
    `bool` is rejected even though it's an `int` subclass, to avoid silent
    `True == 1` confusion in field positions where we expect a number.
    """
    if key not in data:
        raise HandshakeError(f"handshake message missing required field {key!r}")
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise HandshakeError(f"handshake field {key!r} expected int, got {type(value).__name__}")
    return int(value)


def _require_bool(data: dict[str, Any], key: str) -> bool:
    if key not in data:
        raise HandshakeError(f"handshake message missing required field {key!r}")
    value = data[key]
    if not isinstance(value, bool):
        raise HandshakeError(f"handshake field {key!r} expected bool, got {type(value).__name__}")
    return value


def _require_str(data: dict[str, Any], key: str) -> str:
    if key not in data:
        raise HandshakeError(f"handshake message missing required field {key!r}")
    value = data[key]
    if not isinstance(value, str):
        raise HandshakeError(f"handshake field {key!r} expected str, got {type(value).__name__}")
    return value


def _optional_str(data: dict[str, Any], key: str, default: str = "") -> str:
    """Pull a `QString` field that older cores may omit. Type-checked when present."""
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise HandshakeError(f"handshake field {key!r} expected str, got {type(value).__name__}")
    return value


def _optional_int(data: dict[str, Any], key: str) -> int | None:
    """Pull an int field that may be absent. `bool` is rejected as for `_require_int`."""
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise HandshakeError(f"handshake field {key!r} expected int, got {type(value).__name__}")
    return int(value)


def _optional_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Pull a `QVariantMap` field that may be absent. Empty dict on missing/null."""
    if key not in data or data[key] is None:
        return {}
    value = data[key]
    if not isinstance(value, dict):
        raise HandshakeError(f"handshake field {key!r} expected dict, got {type(value).__name__}")
    return dict(value)


def _optional_str_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    """Pull a `QStringList` out of a handshake map, defaulting to empty.

    Missing is fine — `FeatureList` was not present on older cores. A
    present-but-non-list value is a protocol error. Each element must be a
    `str` after the QStringList codec, anything else fails loudly.
    """
    if key not in data:
        return ()
    value = data[key]
    if value is None:
        return ()
    if not isinstance(value, list):
        raise HandshakeError(f"handshake field {key!r} expected list, got {type(value).__name__}")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise HandshakeError(
                f"handshake field {key!r}[{i}] expected str, got {type(item).__name__}"
            )
        out.append(item)
    return tuple(out)


def _optional_dict_list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Pull a `QVariantList` of `QVariantMap` out of a handshake map.

    Used for `StorageBackends` / `Authenticators`. Missing or null is an
    empty list. Non-dict elements are dropped with no error — a forward-
    compatible core may add new entries with shapes we don't recognize and
    we'd rather skip them than crash the handshake.
    """
    if key not in data:
        return []
    value = data[key]
    if value is None:
        return []
    if not isinstance(value, list):
        raise HandshakeError(f"handshake field {key!r} expected list, got {type(value).__name__}")
    return [item for item in value if isinstance(item, dict)]


@dataclass(frozen=True, slots=True)
class ClientInit:
    """The first framed message a client sends to the core.

    `features` is the legacy feature-bits quint32 that pre-modern cores
    look at; modern cores prefer `feature_list` (a list of stringly named
    features). We send both so that we work against both. An empty
    `feature_list` plus `features=0` makes us look like a minimum-viable
    client to the core, which is fine — we opt into features as we
    implement support for them.
    """

    client_version: str
    build_date: str
    features: int = 0
    feature_list: tuple[str, ...] = ()

    def to_map(self) -> dict[str, Any]:
        return {
            "MsgType": CLIENT_INIT,
            "ClientVersion": self.client_version,
            "ClientDate": self.build_date,
            "Features": self.features,
            "FeatureList": list(self.feature_list),
        }


@dataclass(frozen=True, slots=True)
class StorageBackendInfo:
    """One entry from the `StorageBackends` list in `ClientInitAck`.

    The core advertises every backend it was compiled against so a not-yet-
    configured core can ask the client to pick one during initial setup.
    A configured core still sends this list — we just ignore it, but the
    `--probe-only` CLI prints it for human inspection.
    """

    display_name: str
    description: str
    setup_keys: tuple[str, ...]
    setup_defaults: dict[str, Any]
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_map(cls, data: dict[str, Any]) -> StorageBackendInfo:
        return cls(
            display_name=_optional_str(data, "DisplayName"),
            description=_optional_str(data, "Description"),
            setup_keys=_optional_str_list(data, "SetupKeys"),
            setup_defaults=_optional_dict(data, "SetupDefaults"),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class AuthenticatorInfo:
    """One entry from the `Authenticators` list in `ClientInitAck`.

    Same shape as `StorageBackendInfo`. Older cores omit this field entirely;
    we treat its absence as an empty list.
    """

    display_name: str
    description: str
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_map(cls, data: dict[str, Any]) -> AuthenticatorInfo:
        return cls(
            display_name=_optional_str(data, "DisplayName"),
            description=_optional_str(data, "Description"),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class ClientInitAck:
    """The core's response when our ClientInit is acceptable.

    `core_features` mirrors `ClientInit.features` — the legacy quint32 the
    core supports. `feature_list` is the modern stringly-named equivalent.
    `configured` tells us whether the core has had its initial setup done;
    if False the core expects a `CoreSetupData` message rather than a
    `ClientLogin`. We only support the `configured=True` path in v1.

    `protocol_version` is a quint8 that early cores sent and modern cores
    still echo; we record it but don't act on it.
    """

    core_features: int
    feature_list: tuple[str, ...]
    configured: bool
    storage_backends: tuple[StorageBackendInfo, ...]
    authenticators: tuple[AuthenticatorInfo, ...]
    protocol_version: int | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_map(cls, data: dict[str, Any]) -> ClientInitAck:
        return cls(
            core_features=_require_int(data, "CoreFeatures"),
            feature_list=_optional_str_list(data, "FeatureList"),
            configured=_require_bool(data, "Configured"),
            storage_backends=tuple(
                StorageBackendInfo.from_map(b) for b in _optional_dict_list(data, "StorageBackends")
            ),
            authenticators=tuple(
                AuthenticatorInfo.from_map(a) for a in _optional_dict_list(data, "Authenticators")
            ),
            protocol_version=_optional_int(data, "ProtocolVersion"),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class ClientInitReject:
    """The core refused our ClientInit.

    `error_string` is a human-readable message — typically a version
    incompatibility or an outright "core too old / too new". This is a
    terminal failure; we should not retry without changing something.
    """

    error_string: str
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_map(cls, data: dict[str, Any]) -> ClientInitReject:
        return cls(
            error_string=_optional_str(data, "Error"),
            raw=data,
        )


def parse_handshake_message(data: dict[str, Any]) -> ClientInitAck | ClientInitReject:
    """Dispatch a freshly-decoded handshake map to its dataclass.

    Phase 2 only knows the post-ClientInit messages (Ack/Reject). The
    function's union return type will grow as later phases add ClientLogin
    and SessionInit dispatch.

    Raises `HandshakeError` if `MsgType` is missing or unrecognized — both
    indicate the core sent something we don't know how to handle, which is
    a protocol-level bug we want to surface loudly rather than silently
    drop.
    """
    msg_type = data.get("MsgType")
    if msg_type is None:
        raise HandshakeError("handshake message has no MsgType field")
    if msg_type == CLIENT_INIT_ACK:
        return ClientInitAck.from_map(data)
    if msg_type == CLIENT_INIT_REJECT:
        return ClientInitReject.from_map(data)
    raise HandshakeError(f"unknown handshake MsgType {msg_type!r}")
