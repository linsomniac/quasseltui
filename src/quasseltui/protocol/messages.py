"""Typed dataclasses for the handshake-state messages.

The handshake state of a Quassel connection consists of a small fixed set of
QVariantMap-shaped messages exchanged in a fixed order:

    Client → ClientInit              ──┐
    Core   → ClientInitAck             │  setup
              or ClientInitReject      │
    Client → ClientLogin               │  auth
    Core   → ClientLoginAck            │
              or ClientLoginReject     │
    Core   → SessionInit             ──┘  setup → connected

Each message is a flat key→value map on the wire. We model each as a
dataclass plus a from-dict / to-dict converter so the rest of the protocol
layer never has to reach into raw QVariantMaps. Phase 3 fills in the
ClientLogin → SessionInit half of the handshake; phase 2 covered the
ClientInit half.
"""

from __future__ import annotations

# `quasseltui.protocol.usertypes` is imported below for its side effect of
# registering the Quassel user-type codecs (`BufferInfo`, `BufferId`,
# `NetworkId`, ...) into `quasseltui.qt.usertypes` so that `read_variant`
# can decode them when a `SessionInit` message comes in.
from dataclasses import dataclass, field
from typing import Any

from quasseltui.protocol.errors import AuthError, HandshakeError
from quasseltui.protocol.usertypes import BufferInfo, NetworkId

CLIENT_INIT = "ClientInit"
CLIENT_INIT_ACK = "ClientInitAck"
CLIENT_INIT_REJECT = "ClientInitReject"
CLIENT_LOGIN = "ClientLogin"
CLIENT_LOGIN_ACK = "ClientLoginAck"
CLIENT_LOGIN_REJECT = "ClientLoginReject"
SESSION_INIT = "SessionInit"
CORE_SETUP_DATA = "CoreSetupData"
CORE_SETUP_ACK = "CoreSetupAck"
CORE_SETUP_REJECT = "CoreSetupReject"


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


@dataclass(frozen=True, slots=True)
class ClientLogin:
    """Outbound credential message — second half of the handshake.

    Only username and password go on the wire. Modern Quassel cores can be
    configured with external authenticators (LDAP, PAM, ...) but the client
    side of the protocol is always the same `User`/`Password` pair. The
    core dispatches to the configured authenticator internally.
    """

    user: str
    password: str

    def to_map(self) -> dict[str, Any]:
        return {
            "MsgType": CLIENT_LOGIN,
            "User": self.user,
            "Password": self.password,
        }


@dataclass(frozen=True, slots=True)
class ClientLoginAck:
    """The core accepted our credentials. No payload — just the MsgType.

    The very next message we'll get is `SessionInit`. We model this as a
    real dataclass anyway so callers can use `match` / `isinstance` rather
    than poking at strings.
    """

    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_map(cls, data: dict[str, Any]) -> ClientLoginAck:
        return cls(raw=data)


@dataclass(frozen=True, slots=True)
class ClientLoginReject:
    """The core refused our credentials.

    Surfaced through `AuthError` rather than `HandshakeError` so the
    eventual reconnect supervisor (phase 11) can detect "do not retry"
    distinctly from network-level failures.
    """

    error_string: str
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_map(cls, data: dict[str, Any]) -> ClientLoginReject:
        return cls(
            error_string=_optional_str(data, "Error"),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class CoreSetupReject:
    """The core's response to a CoreSetupData wizard failure.

    We never send `CoreSetupData` ourselves (we assume the core is already
    configured), but the parser knows about this message type so the CLI
    can print a useful error if a user accidentally points us at a
    fresh-out-of-the-box core.
    """

    error_string: str
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_map(cls, data: dict[str, Any]) -> CoreSetupReject:
        return cls(
            error_string=_optional_str(data, "Error"),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class SessionInit:
    """The first message after a successful login: the world we just joined.

    Quassel ships this as `{"SessionState": {"Identities": [...],
    "BufferInfos": [...], "NetworkIds": [...]}}`. We unpack the inner map
    and parse the typed lists eagerly:

    - `network_ids` is a list of `NetworkId` user-type instances.
    - `buffer_infos` is a list of `BufferInfo` user-type instances.
    - `identities` stays as a list of raw dicts; phase 5 will model
      Identity properly when the syncable object layer arrives. Counting
      and printing the display names is enough for phase 3.
    """

    identities: tuple[dict[str, Any], ...]
    network_ids: tuple[NetworkId, ...]
    buffer_infos: tuple[BufferInfo, ...]
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_map(cls, data: dict[str, Any]) -> SessionInit:
        if "SessionState" not in data:
            raise HandshakeError("SessionInit message missing required field 'SessionState'")
        state = data["SessionState"]
        if not isinstance(state, dict):
            raise HandshakeError(
                f"SessionInit field 'SessionState' expected dict, got {type(state).__name__}"
            )

        identities = _list_of_dicts(state, "Identities")
        network_ids = _list_of(state, "NetworkIds", NetworkId, "NetworkId")
        buffer_infos = _list_of(state, "BufferInfos", BufferInfo, "BufferInfo")

        return cls(
            identities=tuple(identities),
            network_ids=tuple(network_ids),
            buffer_infos=tuple(buffer_infos),
            raw=data,
        )


def _list_of_dicts(state: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Pull a `QVariantList<QVariantMap>` out of a SessionState sub-map.

    Missing or null is an empty list. Non-dict elements raise — the only
    field we use this for (`Identities`) really should be a list of maps,
    and a different shape means the core is sending something we don't
    understand. Crashing here surfaces the bug instead of silently dropping
    identities.
    """
    if key not in state or state[key] is None:
        return []
    value = state[key]
    if not isinstance(value, list):
        raise HandshakeError(
            f"SessionState field {key!r} expected list, got {type(value).__name__}"
        )
    out: list[dict[str, Any]] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise HandshakeError(
                f"SessionState field {key!r}[{i}] expected dict, got {type(item).__name__}"
            )
        out.append(item)
    return out


def _list_of(
    state: dict[str, Any],
    key: str,
    expected_cls: type,
    expected_name: str,
) -> list[Any]:
    """Pull a typed `QVariantList<UserType>` out of a SessionState sub-map.

    Each element must already be an instance of `expected_cls` — the
    QVariant decoder turns `QVariant<NetworkId>` envelopes into our
    `NetworkId` dataclass, so by the time we see the list it should be
    homogeneous. Anything else is a protocol error.
    """
    if key not in state or state[key] is None:
        return []
    value = state[key]
    if not isinstance(value, list):
        raise HandshakeError(
            f"SessionState field {key!r} expected list, got {type(value).__name__}"
        )
    out: list[Any] = []
    for i, item in enumerate(value):
        if not isinstance(item, expected_cls):
            raise HandshakeError(
                f"SessionState field {key!r}[{i}] expected {expected_name}, "
                f"got {type(item).__name__}"
            )
        out.append(item)
    return out


HandshakeMessage = (
    ClientInitAck
    | ClientInitReject
    | ClientLoginAck
    | ClientLoginReject
    | SessionInit
    | CoreSetupReject
)


def parse_handshake_message(data: dict[str, Any]) -> HandshakeMessage:
    """Dispatch a freshly-decoded handshake map to its dataclass.

    `ClientLoginReject` is converted to an `AuthError` exception rather
    than returned as a value, because credential failures are terminal and
    the rest of the connection state machine should never see a
    `ClientLoginReject` "result" — bouncing through an exception forces
    callers to handle it instead of forgetting and continuing.

    Raises `HandshakeError` if `MsgType` is missing or unrecognized, and
    `AuthError` for `ClientLoginReject`.
    """
    msg_type = data.get("MsgType")
    if msg_type is None:
        raise HandshakeError("handshake message has no MsgType field")
    if msg_type == CLIENT_INIT_ACK:
        return ClientInitAck.from_map(data)
    if msg_type == CLIENT_INIT_REJECT:
        return ClientInitReject.from_map(data)
    if msg_type == CLIENT_LOGIN_ACK:
        return ClientLoginAck.from_map(data)
    if msg_type == CLIENT_LOGIN_REJECT:
        rejected = ClientLoginReject.from_map(data)
        raise AuthError(rejected.error_string or "core rejected credentials")
    if msg_type == SESSION_INIT:
        return SessionInit.from_map(data)
    if msg_type == CORE_SETUP_REJECT:
        return CoreSetupReject.from_map(data)
    raise HandshakeError(f"unknown handshake MsgType {msg_type!r}")
