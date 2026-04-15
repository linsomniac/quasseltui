"""Translate `ClientEvent`s into Textual `Message`s for the running app.

The bridge is the only place in the codebase that knows both
`quasseltui.client` (L4) and `quasseltui.app` (L5). It runs as a Textual
worker inside `QuasselApp.on_mount`; its single task is to iterate
`QuasselClient.events()` forever and post narrow Textual messages
(`BufferListUpdated`, `ActiveBufferUpdated`, `SessionEnded`) whenever a
widget needs to redraw.

Concurrency model

- One task. The bridge runs inside Textual's event loop — the same
  loop as the widgets — so there are no threads, no queues, no
  `call_from_thread`. Outbound writes from phase 9 will happen on the
  same loop too.
- The protocol read loop (inside `client.events()`) is driven by the
  `async for` in `run()`. Back-pressure is natural: if the bridge is
  slow, the read loop doesn't advance, and the OS socket buffer
  absorbs what the core sends.

Refresh-storm protection

A busy channel can produce hundreds of `MessageReceived` events per
second. If every one triggered a full `MessageLog` re-read + Textual
redraw, the terminal would stutter under `#python`-class load. Instead
we coalesce `MessageReceived` for the currently active buffer behind a
single 50ms debounce window:

1. First message schedules an `asyncio.sleep(0.05)` task.
2. Further messages inside that window are absorbed — the task is
   already pending, so we skip creating another.
3. When the sleep resolves, we post exactly one
   `ActiveBufferUpdated`; future messages start a fresh window.

The debounce is trailing-edge — the user sees a 50ms lag before the
newest message appears, which is well below the ~100ms human flicker
threshold. Messages for non-active buffers are simply dropped from the
UI update path in phase 7; phase 8 will wire up an "unread" indicator
if it turns out we need one.

Testability

The sink is typed as a `MessageSink` protocol: an object that has an
`active_buffer_id` attribute and a `post_message(Message) -> bool`
method. `QuasselApp` satisfies this naturally, and the unit tests in
`tests/unit/app/test_bridge.py` pass a dataclass stub instead of
spinning up a Textual app. The events iterator is also injectable so
tests can feed a hand-built sequence without a live core.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Protocol

from textual.message import Message

from quasseltui.app.messages import (
    ActiveBufferUpdated,
    BufferListUpdated,
    SessionEnded,
)
from quasseltui.client.state import ClientState
from quasseltui.protocol.usertypes import BufferId
from quasseltui.sync.events import (
    BufferAdded,
    BufferRemoved,
    BufferRenamed,
    ClientDisconnected,
    ClientEvent,
    MessageReceived,
    NetworkAdded,
    NetworkRemoved,
    NetworkUpdated,
    SessionOpened,
)

_log = logging.getLogger(__name__)


class MessageSink(Protocol):
    """Structural type for the object the bridge posts messages into.

    `QuasselApp` satisfies this naturally (Textual's `post_message` is
    the canonical dispatch method, and we add `active_buffer_id` as an
    instance attribute on the app). Using a protocol lets the bridge
    tests hand in a lightweight stub that records messages without
    spinning up the full app.
    """

    active_buffer_id: BufferId | None

    def post_message(self, message: Message) -> bool: ...


class ClientBridge:
    """Translate `ClientEvent`s into Textual `Message`s and post them.

    Hold exactly one active debounce task at a time in
    `self._debounce_task`. On normal iterator exhaustion we `await` the
    task so the final coalesced refresh lands; on cancellation (worker
    shutdown) we cancel it so it doesn't outlive the app.
    """

    DEBOUNCE_SECONDS = 0.05
    """Trailing-edge debounce for `MessageReceived` on the active buffer.

    50ms is below human flicker perception (~100ms) but comfortably
    above the ~16ms Textual frame interval, so the coalesced refresh
    lands in the next frame instead of forcing a re-render per message.
    """

    def __init__(
        self,
        *,
        events: AsyncIterator[ClientEvent],
        sink: MessageSink,
        state: ClientState,
        debounce_seconds: float | None = None,
    ) -> None:
        self._events = events
        self._sink = sink
        self._state = state
        self._debounce_seconds = (
            debounce_seconds if debounce_seconds is not None else self.DEBOUNCE_SECONDS
        )
        self._debounce_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Iterate client events and dispatch them to the sink.

        Returns only when the event iterator is exhausted (normal
        terminal-disconnect path) or when the enclosing worker is
        cancelled. On normal exit we wait for any pending debounce
        task so the last coalesced active-buffer refresh is not
        silently dropped. On cancellation the `finally` block cancels
        the debounce task so it doesn't outlive the app.
        """
        try:
            async for event in self._events:
                self._handle(event)
            pending = self._debounce_task
            if pending is not None and not pending.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
        finally:
            pending = self._debounce_task
            if pending is not None and not pending.done():
                pending.cancel()

    # -- dispatch -----------------------------------------------------------

    def _handle(self, event: ClientEvent) -> None:
        """Route one `ClientEvent` to the appropriate sink call(s).

        Order matters inside this method: we always post the
        list-level update (`BufferListUpdated`) *before* any active-
        buffer update, so a handler that redraws the sidebar and then
        the message log sees the new sidebar first. Unknown event
        types (e.g. `IdentityAdded` in phase 7) are silently dropped.
        """
        if isinstance(event, SessionOpened):
            self._sink.post_message(BufferListUpdated())
            self._maybe_pick_default_active_buffer()
            return
        if isinstance(event, BufferAdded | BufferRemoved | BufferRenamed):
            self._sink.post_message(BufferListUpdated())
            self._maybe_pick_default_active_buffer()
            return
        if isinstance(event, NetworkAdded | NetworkRemoved | NetworkUpdated):
            self._sink.post_message(BufferListUpdated())
            return
        if isinstance(event, MessageReceived):
            self._handle_message(event)
            return
        if isinstance(event, ClientDisconnected):
            self._sink.post_message(SessionEnded(reason=event.reason))
            return
        # IdentityAdded and anything else — no UI effect in phase 7.

    def _handle_message(self, event: MessageReceived) -> None:
        """Apply a `MessageReceived` — coalesce if active, ignore if not.

        Calls `_maybe_pick_default_active_buffer` first to cover the
        cold-start case where the very first message precedes any
        explicit buffer pick (the dispatcher appends to
        `state.messages` before emitting, so a default pick at this
        point will see the new content).
        """
        self._maybe_pick_default_active_buffer()
        if event.message.buffer_id != self._sink.active_buffer_id:
            return
        self._schedule_active_refresh()

    def _schedule_active_refresh(self) -> None:
        """Start a debounce task if none is pending.

        A pending task means another refresh is already scheduled
        for the end of the current 50ms window — we just absorb the
        new event into it and return.
        """
        if self._debounce_task is not None and not self._debounce_task.done():
            return
        self._debounce_task = asyncio.create_task(self._debounced_active_refresh())

    async def _debounced_active_refresh(self) -> None:
        """Sleep the debounce window, then post one `ActiveBufferUpdated`."""
        try:
            await asyncio.sleep(self._debounce_seconds)
        except asyncio.CancelledError:
            return
        self._sink.post_message(ActiveBufferUpdated(buffer_id=self._sink.active_buffer_id))

    def _maybe_pick_default_active_buffer(self) -> None:
        """If no buffer is active yet, pick one and post an update.

        Called after every event that might populate state. The
        "prefer a buffer with content" heuristic matches what
        `MessageLog._pick_initial_buffer` does in demo mode — the
        intent is to land on whatever is visually interesting on
        first paint. Once an active buffer is set, this method is a
        no-op until phase 8 wires explicit user selection.
        """
        if self._sink.active_buffer_id is not None:
            return
        new_id = _pick_default_buffer(self._state)
        if new_id is None:
            return
        self._sink.active_buffer_id = new_id
        self._sink.post_message(ActiveBufferUpdated(buffer_id=new_id))


def _pick_default_buffer(state: ClientState) -> BufferId | None:
    """Return the first buffer with any message, or any buffer otherwise.

    Extracted as a free function so the bridge and its tests can share
    the selection logic without reaching into the widget layer. Returns
    `None` only when the state has zero buffers, in which case there's
    nothing sensible to show yet and the bridge leaves the pointer
    unset until `BufferAdded` fires for the first one.
    """
    for buffer_id, messages in state.messages.items():
        if messages:
            return buffer_id
    return next(iter(state.buffers), None)


__all__ = [
    "ClientBridge",
    "MessageSink",
]
