"""Textual `Message` classes the bridge posts into the app.

The bridge reads `ClientEvent`s from `QuasselClient` and translates them
into these narrow, UI-focused messages. Each one represents "a redraw
is needed in widget X" — the actual redraw logic lives in widget
methods, which the app invokes from the message handlers.

Why these live in a separate file from `sync.events`:

- `sync.events` is the cross-layer event boundary consumed by any
  embedder of `QuasselClient`. Those dataclasses have zero Textual
  dependency so the lower layers stay importable without pulling
  Textual into the process.
- The messages in this file are *inside* L5 — Textual-specific and
  only meaningful within the running app. Keeping them separate from
  `sync.events` keeps the import-linter contract clean (only
  `quasseltui.app.*` imports `textual`).
"""

from __future__ import annotations

from textual.message import Message

from quasseltui.protocol.usertypes import BufferId


class SessionStarted(Message):
    """Fired once, when the bridge sees its first `SessionOpened`.

    Lets the app distinguish an early fatal disconnect (no
    `SessionStarted` before `SessionEnded`) from a mid-session drop
    (one was seen first). Without this signal, a failed handshake or
    auth rejection would leave the user staring at an empty Textual
    screen with no in-app explanation and the process still exiting
    cleanly on Ctrl+Q.
    """


class BufferListUpdated(Message):
    """The sidebar needs to re-read networks/buffers from state.

    Fired when any of `NetworkAdded`, `NetworkRemoved`, `NetworkUpdated`,
    `BufferAdded`, `BufferRemoved`, or `BufferRenamed` arrives. The
    message carries no payload — the handler re-reads the whole tree
    from state, which is cheap for the handful of entries a typical
    session has and avoids any diff logic that could drift out of sync.
    """


class ActiveBufferUpdated(Message):
    """The message log needs to re-read the active buffer's history.

    Fired when the active buffer pointer changes OR when new messages
    have arrived for the currently-active buffer (after the 50ms
    debounce window closes). Both cases collapse to the same action:
    re-read `state.messages[buffer_id]` and render.

    Carries the new active `buffer_id` so the handler does not have to
    reach back into shared state for it — if the pointer and the
    message disagree later, the message is authoritative.
    """

    def __init__(self, buffer_id: BufferId | None) -> None:
        super().__init__()
        self.buffer_id = buffer_id


class SessionEnded(Message):
    """The live client disconnected — terminal for the session.

    Phase 7 just logs this and leaves the last-known state on screen so
    the user can still scroll the history they saw. Phase 11 will
    surface it via a status bar notification and (optionally) feed a
    reconnect supervisor.
    """

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason


__all__ = [
    "ActiveBufferUpdated",
    "BufferListUpdated",
    "SessionEnded",
    "SessionStarted",
]
