"""Quassel protocol enums and feature names.

Mirrors the runtime-protocol-relevant enums from the Quassel C++ source:

- `MessageType` and `MessageFlag` live in `src/common/message.h` and govern
  the IRC message struct on the wire.
- `Feature` is the stringly-named feature flag list from `src/common/quassel.h`.
  Each feature corresponds to a specific behavior toggle that both peers must
  agree on at handshake time. We track only the names we actually opt into.

The plan calls these out in `src/quasseltui/protocol/enums.py` (and notes that
`ConnState` lives in `connection.py` because it's a state-machine concept,
not a wire enum).
"""

from __future__ import annotations

from enum import IntEnum, IntFlag


class MessageType(IntEnum):
    """Mirror of `Message::Type` in `src/common/message.h`.

    These are bit-shaped values (0x1, 0x2, 0x4, ...) — the C++ uses
    `Q_DECLARE_FLAGS` so a single message holds exactly one bit, but the
    wire representation is `quint32` so a hypothetical compound type would
    parse without error. We keep `IntEnum` semantics; if Quassel ever ships
    a compound type we'll see an `unknown` and degrade gracefully.
    """

    Plain = 0x00001
    Notice = 0x00002
    Action = 0x00004
    Nick = 0x00008
    Mode = 0x00010
    Join = 0x00020
    Part = 0x00040
    Quit = 0x00080
    Kick = 0x00100
    Kill = 0x00200
    Server = 0x00400
    Info = 0x00800
    Error = 0x01000
    DayChange = 0x02000
    Topic = 0x04000
    NetsplitJoin = 0x08000
    NetsplitQuit = 0x10000
    Invite = 0x20000


class MessageFlag(IntFlag):
    """Mirror of `Message::Flag` in `src/common/message.h`.

    Real bitfield — multiple flags can be set on one message. Stored as
    `quint8` on the wire even though only 8 bits are defined today.
    """

    NONE = 0x00
    Self = 0x01
    Highlight = 0x02
    Redirected = 0x04
    ServerMsg = 0x08
    StatusMsg = 0x10
    Ignored = 0x20
    Backlog = 0x80


# ---------------------------------------------------------------------------
# Feature-flag names, from `Quassel::Feature` in `src/common/quassel.h`.
#
# These are stringly-named in the wire format: `ClientInit.FeatureList` is a
# `QStringList` and the negotiated set is the intersection of what both sides
# advertise. The Message user-type's wire shape depends on three of these
# specifically (`LongTime`, `SenderPrefixes`, `RichMessages`); the others
# don't change the byte layout, so we list them here for documentation but
# don't currently advertise them.
# ---------------------------------------------------------------------------


# Modern cores write `quint64` ms-since-epoch timestamps (instead of the
# legacy `quint32` seconds-since-epoch) when this is negotiated. We always
# want it — qint32 seconds wraps in 2038.
FEATURE_LONG_TIME = "LongTime"

# Adds a `senderPrefixes` field (the IRC modes prefix like `@` or `+`) to
# Message::operator<<. Cheap, useful for rendering — opt in.
FEATURE_SENDER_PREFIXES = "SenderPrefixes"

# Adds `realName` and `avatarUrl` to Message::operator<<. We don't display
# either today but advertising them keeps us forward-compatible if/when the
# UI adds richer message rendering.
FEATURE_RICH_MESSAGES = "RichMessages"

# Promotes `MsgId` from qint32 to qint64. We always read MsgId as qint64 —
# advertising this just tells the core not to bother with the legacy path.
FEATURE_LONG_MESSAGE_ID = "LongMessageId"

# All advertised features, in the order ClientInit's FeatureList sends them.
# Kept as a tuple so callers can be sure nobody mutates it.
DEFAULT_CLIENT_FEATURES: tuple[str, ...] = (
    FEATURE_LONG_TIME,
    FEATURE_SENDER_PREFIXES,
    FEATURE_RICH_MESSAGES,
    FEATURE_LONG_MESSAGE_ID,
)


# ---------------------------------------------------------------------------
# Legacy binary feature flags from `Quassel::Feature` (pre-string-list era).
#
# These correspond to the `Features` / `CoreFeatures` quint32 bitmask in
# `ClientInit` / `ClientInitAck`. Older cores that predate string-based
# negotiation (`FeatureList`) only check these bits to decide which
# optional wire fields to include.
#
# Only features that affect the wire format we care about are mapped.
# `LongTime`, `RichMessages`, and `LongMessageId` have no legacy bit —
# they were introduced after the string-based system.
# ---------------------------------------------------------------------------

# AIDEV-NOTE: Bit positions from `enum class Feature` in
# `src/common/quassel.h`. SenderPrefixes is the only one with both a
# legacy bit and a string feature name that affects Message decoding.
LEGACY_SENDER_PREFIXES = 1 << 13  # 0x2000
LEGACY_EXTENDED_FEATURES = 1 << 15  # 0x8000 — signals string-based negotiation

# Maps legacy bit → string feature name (only features we care about).
_LEGACY_TO_STRING: dict[int, str] = {
    LEGACY_SENDER_PREFIXES: FEATURE_SENDER_PREFIXES,
}

# Reverse map for bitmask → feature set conversion.
_STRING_TO_LEGACY: dict[str, int] = {v: k for k, v in _LEGACY_TO_STRING.items()}


def features_to_bitmask(features: tuple[str, ...] | frozenset[str]) -> int:
    """Compute the legacy binary feature bitmask from string feature names.

    Only features with a known legacy bit mapping are included.
    ``ExtendedFeatures`` (bit 15) is deliberately NOT set — setting it
    can cause some older cores to change their ``ClientInitAck`` format
    in ways we don't handle. The string-based ``FeatureList`` in
    ``ClientInit`` is sufficient for modern cores.
    """
    mask = 0
    for name in features:
        bit = _STRING_TO_LEGACY.get(name)
        if bit is not None:
            mask |= bit
    return mask


def bitmask_to_features(bitmask: int) -> frozenset[str]:
    """Extract string feature names from a legacy binary bitmask.

    Returns only the features that have a known string equivalent.
    """
    result: set[str] = set()
    for bit, name in _LEGACY_TO_STRING.items():
        if bitmask & bit:
            result.add(name)
    return frozenset(result)


__all__ = [
    "DEFAULT_CLIENT_FEATURES",
    "FEATURE_LONG_MESSAGE_ID",
    "FEATURE_LONG_TIME",
    "FEATURE_RICH_MESSAGES",
    "FEATURE_SENDER_PREFIXES",
    "LEGACY_EXTENDED_FEATURES",
    "LEGACY_SENDER_PREFIXES",
    "MessageFlag",
    "MessageType",
    "bitmask_to_features",
    "features_to_bitmask",
]
