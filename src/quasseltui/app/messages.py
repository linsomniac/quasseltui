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

from quasseltui.protocol.usertypes import BufferId, MsgId


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


class BufferSelected(Message):
    """The user asked to switch the active buffer.

    Posted by the `BufferTree` when a leaf is clicked or Enter is
    pressed on it, and also by the app's own alt+up/alt+down
    cycle-buffer actions. The app handler is the single place that
    flips `QuasselApp.active_buffer_id` and posts the follow-up
    `ActiveBufferUpdated`, so the tree and the cycling bindings share
    one code path and can't drift from each other.
    """

    def __init__(self, buffer_id: BufferId) -> None:
        super().__init__()
        self.buffer_id = buffer_id


class LineSubmitted(Message):
    """The user pressed Enter in the input bar.

    `InputBar` posts this with the current line contents and
    immediately clears the widget. If the app's `send_input` fails,
    the app restores the text so the user can retry. The app handler
    is responsible for routing the text to `QuasselClient.send_input`
    — the widget stays dumb and has no client reference of its own.
    Phase 11 will slot /-command parsing into the same handler
    without widening the message.

    Named `LineSubmitted` rather than `InputSubmitted` on purpose:
    Textual derives handler method names from the message class name
    (snake-cased), and `InputSubmitted` would collide with the
    built-in `on_input_submitted` handler for `Input.Submitted`,
    causing Textual to route both messages to the same method and
    trip over `event.value` not existing on our custom class.
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class ReadMarkerPlaced(Message):
    """The user asked to drop a "read up to here" marker on a message.

    Posted by `MessageLog` when Enter fires on a highlighted message
    row. The app handler writes `state.read_markers[buffer_id] = msg_id`
    (replacing any existing marker for that buffer — there is only ever
    one marker per buffer) and triggers a log rebuild so the new
    position renders. Per-buffer, in-memory only: markers reset on app
    restart, which is deliberate — persistence across restarts would
    need a durable local store that phase 10 doesn't have.
    """

    def __init__(self, buffer_id: BufferId, msg_id: MsgId) -> None:
        super().__init__()
        self.buffer_id = buffer_id
        self.msg_id = msg_id


class MarkerToLatestRequested(Message):
    """The user asked to drop a marker at the newest message in the active buffer.

    Posted by `InputBar` when the user presses Enter with no text typed.
    The app handler resolves "latest" against `state.messages[active]`
    and writes `state.read_markers[active] = last.msg_id`, then triggers
    a log redraw. No-op when there is no active buffer or the buffer is
    empty. Complements `ReadMarkerPlaced` (which targets a specific
    highlighted row inside `MessageLog`); this path exists so a user
    who never Tabs into the log can still place a marker with the
    keyboard alone.
    """


class SessionEnded(Message):
    """The live client disconnected.

    `fatal` is `True` when the disconnect happened before the bridge
    ever saw a `SessionOpened` — i.e. a failed handshake, auth reject,
    TLS error, or any other pre-session failure that the user would
    otherwise stare at a blank app over. `False` for a mid-session
    drop, which the UI leaves on screen (the user can still scroll
    history and quit via Ctrl+Q).

    The flag is computed by the bridge, not inferred from message
    ordering at the app layer, so the "fatal vs. drop" policy isn't
    timing-coupled and is straightforward to extend in later phases
    (reconnect supervisor, status bar, etc.).
    """

    def __init__(self, reason: str, *, fatal: bool) -> None:
        super().__init__()
        self.reason = reason
        self.fatal = fatal


__all__ = [
    "ActiveBufferUpdated",
    "BufferListUpdated",
    "BufferSelected",
    "LineSubmitted",
    "MarkerToLatestRequested",
    "ReadMarkerPlaced",
    "SessionEnded",
]
