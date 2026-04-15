"""Exception hierarchy for the protocol layer.

These are distinct from the lower-level `QDataStreamError` (which signals a
malformed binary payload) — these mean the protocol *behavior* went wrong:
the peer sent something we can't make sense of, the negotiation failed, or
the connection died at an inopportune moment.
"""

from __future__ import annotations


class QuasselError(Exception):
    """Base class for all protocol-layer errors."""


class ProbeError(QuasselError):
    """The pre-framing probe handshake failed.

    Examples: server didn't speak Quassel, magic mismatch, no protocol in
    common, the connection dropped before we got a reply.
    """


class HandshakeError(QuasselError):
    """A framed handshake message was malformed or the core rejected us.

    Distinct from `AuthError`: this fires for `ClientInitReject` (version
    incompatible, core not configured), missing required fields, or unknown
    `MsgType` values during the HANDSHAKE state.
    """


class AuthError(HandshakeError):
    """The core rejected our credentials (`ClientLoginReject`).

    Carried separately from `HandshakeError` because reconnect supervisors
    must NOT retry on auth failures — they should bounce to the credential
    entry screen instead.
    """


class ConnectionClosed(QuasselError):
    """The peer closed the connection mid-stream."""
