"""Text-sanitization helpers for displaying untrusted data.

The Quassel core (and by extension every IRC user whose traffic passes
through it) is a trust boundary. Every string we read off the wire —
nicks, channel names, topics, message bodies, real names, server
notices — can be attacker-controlled, and IRC has a decades-long
history of being used to smuggle terminal escape sequences into
clients. A sloppy client that prints raw message bodies can be made
to:

- rewrite the scrollback with `\\r` / `\\x08`,
- beep the terminal on every message with `\\x07` (BEL),
- recolor or blank subsequent output with ANSI CSI,
- retitle the terminal window or inject hyperlinks with OSC,
- impersonate another nick by prepending a cleared line.

Both the `dump-state` CLI path and the Textual widgets render these
strings to the terminal, so they share one sanitizer — moving it to a
shared util module means there is exactly one regex to audit and keep
correct.

We replace every unsafe byte with its `\\xNN` literal rather than
dropping it, so an operator debugging a misbehaving core can see what
was there. That's less aggressive than a full strip, but safe because
the escape form is itself printable ASCII.
"""

from __future__ import annotations

import re

# C0 control characters (0x00-0x1f), DEL (0x7f), and C1 controls (0x80-0x9f).
# ESC (0x1b) is the start of every ANSI CSI / OSC sequence; 0x80-0x9f are the
# 8-bit C1 CSI equivalents some terminals still honor; LF/CR/BEL/BS can rewrite
# or spoof preceding output. No legitimate IRC text (channel names, nicks,
# even `résumé`-style non-ASCII content) contains a byte in this range, so
# escaping it unconditionally is safe.
_TERMINAL_UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize_terminal(text: str) -> str:
    """Escape C0/C1 control characters so a terminal cannot interpret them.

    Idempotent on printable ASCII/UTF-8 input and on already-sanitized
    output (the `\\xNN` form is itself all-printable), so callers do
    not need to track whether a string has been sanitized yet. The
    common case — plain text with no control bytes — short-circuits
    through `re.sub` without allocating.
    """
    return _TERMINAL_UNSAFE_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


__all__ = [
    "sanitize_terminal",
]
