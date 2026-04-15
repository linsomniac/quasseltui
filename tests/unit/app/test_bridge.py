"""Unit tests for `quasseltui.app.bridge.ClientBridge`.

Tests are deliberately Textual-free: they feed a hand-built async
iterator into the bridge and assert on the messages a stub sink
receives. This keeps them fast and pinpoints translation-layer
regressions without coupling to widget composition. The real-app
smoke test (bridge → widget → render) lives alongside the existing
`test_chat_screen.py` pilot tests.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from textual.message import Message

from quasseltui.app.bridge import ClientBridge, _pick_default_buffer
from quasseltui.app.messages import (
    ActiveBufferUpdated,
    BufferListUpdated,
    SessionEnded,
)
from quasseltui.client.state import ClientState
from quasseltui.protocol.enums import MessageFlag, MessageType
from quasseltui.protocol.messages import SessionInit
from quasseltui.protocol.usertypes import (
    BufferId,
    BufferInfo,
    BufferType,
    IdentityId,
    MsgId,
    NetworkId,
)
from quasseltui.sync.events import (
    BufferAdded,
    BufferRemoved,
    BufferRenamed,
    ClientDisconnected,
    ClientEvent,
    IdentityAdded,
    IrcMessage,
    MessageReceived,
    NetworkAdded,
    NetworkRemoved,
    NetworkUpdated,
    SessionOpened,
)


@dataclass
class _StubSink:
    """Minimal `MessageSink` implementation for bridge tests.

    Holds the posted messages in a list so tests can assert on order
    and contents. `active_buffer_id` is a plain attribute — the bridge
    writes to it during default-pick, tests read it to check the
    final value.
    """

    active_buffer_id: BufferId | None = None
    posted: list[Message] = field(default_factory=list)

    def post_message(self, message: Message) -> bool:
        self.posted.append(message)
        return True


async def _iter(*events: ClientEvent) -> AsyncIterator[ClientEvent]:
    """Build an async iterator from a finite list of events.

    Used in place of `QuasselClient.events()` for every test in this
    module. Exhausting the iterator is what tells `ClientBridge.run`
    to drain its pending debounce task and return.
    """
    for event in events:
        yield event


def _buffer_info(bid: int, nid: int = 1, name: str = "#test") -> BufferInfo:
    return BufferInfo(
        buffer_id=BufferId(bid),
        network_id=NetworkId(nid),
        type=BufferType.Channel,
        group_id=0,
        name=name,
    )


def _irc_message(bid: int, *, nid: int = 1, msg_id: int = 1) -> IrcMessage:
    return IrcMessage(
        msg_id=MsgId(msg_id),
        buffer_id=BufferId(bid),
        network_id=NetworkId(nid),
        timestamp=dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.UTC),
        type=MessageType.Plain,
        flags=MessageFlag.NONE,
        sender="alice",
        sender_prefixes="",
        contents="hi",
    )


def _buffer_added(bid: int, *, nid: int = 1, name: str = "#test") -> BufferAdded:
    return BufferAdded(
        buffer_id=BufferId(bid),
        network_id=NetworkId(nid),
        name=name,
        type=BufferType.Channel,
    )


def _state_with_buffers(*bufs: BufferInfo) -> ClientState:
    """Build a `ClientState` containing exactly the given buffers.

    Every buffer also gets an empty message list so the bridge's
    default-pick heuristic (prefer a buffer with content) behaves the
    same way it would against a real dispatched state.
    """
    state = ClientState()
    for buf in bufs:
        state.buffers[buf.buffer_id] = buf
        state.messages[buf.buffer_id] = []
    return state


def _types_of(messages: list[Message]) -> list[str]:
    return [type(m).__name__ for m in messages]


class TestBridgeTranslation:
    async def test_session_opened_posts_session_started_first(self) -> None:
        """Order matters: `SessionStarted` must precede `BufferListUpdated`.

        The app uses `SessionStarted` to latch its "has a live
        session" flag; if a later `SessionEnded` arrives *before*
        that flag is set, the app treats it as a fatal startup
        failure. Posting `BufferListUpdated` first would give the
        app a confusing "tree refresh with no session yet" signal
        and would not change the fatal-vs-drop decision anyway.
        """
        buf = _buffer_info(1)
        state = _state_with_buffers(buf)
        sink = _StubSink()
        session = SessionInit(identities=(), network_ids=(), buffer_infos=(), raw={})
        bridge = ClientBridge(
            events=_iter(SessionOpened(session=session, peer_features=frozenset())),
            sink=sink,
            state=state,
        )
        await bridge.run()
        # Expected order: the session-handshake banner, then the
        # sidebar refresh, then the default-pick active-buffer update.
        assert _types_of(sink.posted) == [
            "SessionStarted",
            "BufferListUpdated",
            "ActiveBufferUpdated",
        ]

    async def test_buffer_added_posts_buffer_list_updated(self) -> None:
        buf = _buffer_info(1)
        state = _state_with_buffers(buf)
        sink = _StubSink()
        bridge = ClientBridge(
            events=_iter(_buffer_added(1)),
            sink=sink,
            state=state,
        )
        await bridge.run()
        assert _types_of(sink.posted) == ["BufferListUpdated", "ActiveBufferUpdated"]
        assert sink.active_buffer_id == BufferId(1)

    async def test_buffer_removed_posts_buffer_list_updated(self) -> None:
        # Simulate the dispatcher's "delete from state before emitting"
        # order: by the time the bridge sees the event, the buffer is
        # already gone from `state.buffers`. Without this mirror, the
        # default-pick code would still find the removed id and leave
        # the active pointer dangling on stale scrollback.
        state = _state_with_buffers(_buffer_info(1))
        del state.buffers[BufferId(1)]
        state.messages.pop(BufferId(1), None)
        sink = _StubSink(active_buffer_id=BufferId(1))
        bridge = ClientBridge(
            events=_iter(BufferRemoved(buffer_id=BufferId(1))),
            sink=sink,
            state=state,
        )
        await bridge.run()
        assert any(isinstance(m, BufferListUpdated) for m in sink.posted)

    async def test_buffer_renamed_posts_buffer_list_updated(self) -> None:
        state = _state_with_buffers(_buffer_info(1))
        sink = _StubSink(active_buffer_id=BufferId(1))
        bridge = ClientBridge(
            events=_iter(BufferRenamed(buffer_id=BufferId(1), name="#new")),
            sink=sink,
            state=state,
        )
        await bridge.run()
        assert any(isinstance(m, BufferListUpdated) for m in sink.posted)

    async def test_network_events_post_buffer_list_updated(self) -> None:
        sink = _StubSink(active_buffer_id=BufferId(1))
        state = _state_with_buffers(_buffer_info(1))
        bridge = ClientBridge(
            events=_iter(
                NetworkAdded(network_id=NetworkId(1), name="Libera"),
                NetworkUpdated(
                    network_id=NetworkId(1),
                    field_name="network_name",
                    value="Libera.Chat",
                ),
                NetworkRemoved(network_id=NetworkId(1)),
            ),
            sink=sink,
            state=state,
        )
        await bridge.run()
        updates = [m for m in sink.posted if isinstance(m, BufferListUpdated)]
        assert len(updates) == 3

    async def test_client_disconnected_posts_session_ended_with_reason(self) -> None:
        sink = _StubSink()
        bridge = ClientBridge(
            events=_iter(ClientDisconnected(reason="core shut down", error=None)),
            sink=sink,
            state=ClientState(),
        )
        await bridge.run()
        ended = [m for m in sink.posted if isinstance(m, SessionEnded)]
        assert len(ended) == 1
        assert ended[0].reason == "core shut down"

    async def test_identity_added_is_silently_ignored(self) -> None:
        """Phase 7 doesn't surface identities in any widget.

        The dispatcher still emits them as the session seeds, so the
        bridge must not crash on them — it just drops them on the
        floor. Pinning the behaviour here means a future refactor
        that adds an identity handler can't silently regress the
        "no message on bare IdentityAdded" contract.
        """
        sink = _StubSink(active_buffer_id=BufferId(1))
        state = _state_with_buffers(_buffer_info(1))
        bridge = ClientBridge(
            events=_iter(IdentityAdded(identity_id=IdentityId(1), name="default")),
            sink=sink,
            state=state,
        )
        await bridge.run()
        assert sink.posted == []


class TestMessageDebounce:
    async def test_message_received_for_active_buffer_is_coalesced(self) -> None:
        """Three messages on the active buffer → one ActiveBufferUpdated.

        The bridge awaits the pending debounce task on normal
        iterator exhaustion, so the single refresh lands before
        `run()` returns and the test doesn't need a post-run sleep.
        """
        sink = _StubSink(active_buffer_id=BufferId(1))
        state = _state_with_buffers(_buffer_info(1))
        bridge = ClientBridge(
            events=_iter(
                MessageReceived(_irc_message(1, msg_id=100)),
                MessageReceived(_irc_message(1, msg_id=101)),
                MessageReceived(_irc_message(1, msg_id=102)),
            ),
            sink=sink,
            state=state,
            debounce_seconds=0.005,
        )
        await bridge.run()
        active_updates = [m for m in sink.posted if isinstance(m, ActiveBufferUpdated)]
        assert len(active_updates) == 1

    async def test_message_received_for_inactive_buffer_is_ignored(self) -> None:
        """Noise in a non-active buffer must not trigger a refresh."""
        sink = _StubSink(active_buffer_id=BufferId(1))
        state = _state_with_buffers(_buffer_info(1))
        bridge = ClientBridge(
            events=_iter(MessageReceived(_irc_message(99, msg_id=1))),
            sink=sink,
            state=state,
            debounce_seconds=0.005,
        )
        await bridge.run()
        active_updates = [m for m in sink.posted if isinstance(m, ActiveBufferUpdated)]
        assert active_updates == []

    async def test_debounce_task_is_cancelled_on_run_cancellation(self) -> None:
        """A cancelled bridge must not leak a pending debounce task.

        Simulates worker shutdown: we spawn the bridge against an
        iterator that blocks forever after yielding one message, let
        the debounce task start, then cancel the bridge. The debounce
        task must be in the `done()` state (cancelled) when the
        cancellation settles.
        """

        async def one_then_hang() -> AsyncIterator[ClientEvent]:
            yield MessageReceived(_irc_message(1, msg_id=1))
            await asyncio.sleep(10)  # never reached in test

        sink = _StubSink(active_buffer_id=BufferId(1))
        bridge = ClientBridge(
            events=one_then_hang(),
            sink=sink,
            state=_state_with_buffers(_buffer_info(1)),
            debounce_seconds=5.0,
        )
        task = asyncio.create_task(bridge.run())
        # Give the event loop enough iterations to pull the single
        # MessageReceived event and schedule the debounce before we
        # cancel.
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert bridge._debounce_task is not None
        assert bridge._debounce_task.done()


class TestDefaultActivePick:
    async def test_prefers_buffer_with_messages(self) -> None:
        state = ClientState()
        buf_empty = _buffer_info(1, name="#empty")
        buf_full = _buffer_info(2, name="#busy")
        state.buffers[buf_empty.buffer_id] = buf_empty
        state.buffers[buf_full.buffer_id] = buf_full
        state.messages[buf_empty.buffer_id] = []
        state.messages[buf_full.buffer_id] = [_irc_message(2)]
        sink = _StubSink()
        bridge = ClientBridge(
            events=_iter(_buffer_added(2, name="#busy")),
            sink=sink,
            state=state,
        )
        await bridge.run()
        assert sink.active_buffer_id == BufferId(2)

    async def test_falls_back_to_first_buffer_when_none_have_content(self) -> None:
        state = _state_with_buffers(_buffer_info(5))
        sink = _StubSink()
        bridge = ClientBridge(
            events=_iter(_buffer_added(5)),
            sink=sink,
            state=state,
        )
        await bridge.run()
        assert sink.active_buffer_id == BufferId(5)

    async def test_existing_active_buffer_is_not_overwritten(self) -> None:
        """Seeded `active_buffer_id` (e.g. phase-8 user selection) survives.

        Without this, a BufferAdded for an unrelated buffer after the
        user has clicked a specific one would drag the selection back
        to the auto-picked default, which would be maddening.
        """
        state = _state_with_buffers(_buffer_info(1), _buffer_info(2))
        sink = _StubSink(active_buffer_id=BufferId(2))
        bridge = ClientBridge(
            events=_iter(_buffer_added(1)),
            sink=sink,
            state=state,
        )
        await bridge.run()
        assert sink.active_buffer_id == BufferId(2)

    async def test_no_buffers_no_pick(self) -> None:
        """Empty state → no ActiveBufferUpdated, no crash."""
        sink = _StubSink()
        bridge = ClientBridge(
            events=_iter(BufferRemoved(buffer_id=BufferId(99))),
            sink=sink,
            state=ClientState(),
        )
        await bridge.run()
        active_updates = [m for m in sink.posted if isinstance(m, ActiveBufferUpdated)]
        assert active_updates == []
        assert sink.active_buffer_id is None


class TestBufferRemovedRepicksActive:
    """Regression for codex review finding: a removed active buffer must
    not leave `active_buffer_id` pointing at a dead id. Without the
    special-case in `ClientBridge._handle_buffer_removed`, the
    `_maybe_pick_default_active_buffer` short-circuit ("already has an
    active buffer") preserves the stale pointer forever and the UI is
    stuck on scrollback for a buffer the core has already deleted."""

    async def test_active_buffer_removed_switches_to_remaining_buffer(self) -> None:
        state = _state_with_buffers(_buffer_info(1), _buffer_info(2))
        # Dispatcher deletes before emitting — mirror that.
        del state.buffers[BufferId(1)]
        state.messages.pop(BufferId(1), None)
        sink = _StubSink(active_buffer_id=BufferId(1))
        bridge = ClientBridge(
            events=_iter(BufferRemoved(buffer_id=BufferId(1))),
            sink=sink,
            state=state,
        )
        await bridge.run()
        assert sink.active_buffer_id == BufferId(2)
        active_updates = [m for m in sink.posted if isinstance(m, ActiveBufferUpdated)]
        assert len(active_updates) == 1
        assert active_updates[0].buffer_id == BufferId(2)

    async def test_active_buffer_removed_and_none_remain_clears_pointer(self) -> None:
        state = _state_with_buffers(_buffer_info(1))
        del state.buffers[BufferId(1)]
        state.messages.pop(BufferId(1), None)
        sink = _StubSink(active_buffer_id=BufferId(1))
        bridge = ClientBridge(
            events=_iter(BufferRemoved(buffer_id=BufferId(1))),
            sink=sink,
            state=state,
        )
        await bridge.run()
        assert sink.active_buffer_id is None
        # The explicit `None` update lets the app's handler call
        # `log.clear()` — without it, the message log would keep
        # rendering the removed buffer's scrollback.
        active_updates = [m for m in sink.posted if isinstance(m, ActiveBufferUpdated)]
        assert len(active_updates) == 1
        assert active_updates[0].buffer_id is None

    async def test_inactive_buffer_removed_does_not_affect_active(self) -> None:
        state = _state_with_buffers(_buffer_info(1), _buffer_info(2))
        del state.buffers[BufferId(2)]
        state.messages.pop(BufferId(2), None)
        sink = _StubSink(active_buffer_id=BufferId(1))
        bridge = ClientBridge(
            events=_iter(BufferRemoved(buffer_id=BufferId(2))),
            sink=sink,
            state=state,
        )
        await bridge.run()
        # Unchanged — removing an inactive buffer must not retarget
        # the selection or post a spurious ActiveBufferUpdated.
        assert sink.active_buffer_id == BufferId(1)
        active_updates = [m for m in sink.posted if isinstance(m, ActiveBufferUpdated)]
        assert active_updates == []


class TestPickDefaultBufferFreeFunction:
    def test_returns_none_for_empty_state(self) -> None:
        assert _pick_default_buffer(ClientState()) is None

    def test_returns_only_buffer_if_one_exists(self) -> None:
        state = _state_with_buffers(_buffer_info(7))
        assert _pick_default_buffer(state) == BufferId(7)

    def test_prefers_first_buffer_with_messages(self) -> None:
        state = ClientState()
        a = _buffer_info(1)
        b = _buffer_info(2)
        state.buffers[a.buffer_id] = a
        state.buffers[b.buffer_id] = b
        state.messages[a.buffer_id] = []
        state.messages[b.buffer_id] = [_irc_message(2)]
        assert _pick_default_buffer(state) == BufferId(2)
