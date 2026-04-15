"""Quassel `BufferSyncer` syncable — last-seen / marker-line / buffer lifecycle.

`BufferSyncer` is a singleton (object_name `""`) that the core uses to
broadcast buffer-level metadata:

- `LastSeenMsg` / `setLastSeenMsg(bufferId, msgId)` — the last message the
  user has seen in each buffer, used to compute the "unread" divider.
- `MarkerLines` / `setMarkerLine(bufferId, msgId)` — the "here I left off"
  marker the user can shift by pressing a key.
- `removeBuffer(bufferId)` / `renameBuffer(bufferId, name)` /
  `mergeBuffersPermanently(bufferId1, bufferId2)` — lifecycle operations
  that change what's in the buffer list.

We don't send any of the write slots ourselves in phase 5 — that's phase 8+.
We just track the inbound state so `dump-state` can show per-buffer unread
counts relative to `last_seen_by_buffer`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from quasseltui.protocol.usertypes import BufferId, MsgId
from quasseltui.sync.base import SyncObject, init_field, sync_slot


class BufferSyncer(SyncObject):
    CLASS_NAME: ClassVar[bytes] = b"BufferSyncer"

    def __init__(self, object_name: str = "") -> None:
        super().__init__(object_name)
        self.last_seen_by_buffer: dict[int, int] = {}
        self.marker_lines_by_buffer: dict[int, int] = {}
        # Buffer IDs the core told us no longer exist (via removeBuffer
        # or mergeBuffersPermanently). The dispatcher drains this set on
        # every BufferSyncer slot call and emits a `BufferRemoved` for
        # each one, then clears the set back to empty.
        self.removed_buffers: set[int] = set()
        # Buffer IDs the core renamed via `renameBuffer`, keyed by id and
        # mapped to the new name. Drained the same way as `removed_buffers`
        # — the dispatcher mutates `ClientState.buffers` on each entry and
        # emits a `BufferRenamed` public event.
        self.renamed_buffers: dict[int, str] = {}

    # -- slot handlers ------------------------------------------------------

    @sync_slot(b"setLastSeenMsg")
    def _sync_set_last_seen(self, buffer_id: Any, msg_id: Any) -> None:
        bid = _as_int(buffer_id)
        mid = _as_int(msg_id)
        if bid is not None and mid is not None:
            self.last_seen_by_buffer[bid] = mid

    @sync_slot(b"setMarkerLine")
    def _sync_set_marker_line(self, buffer_id: Any, msg_id: Any) -> None:
        bid = _as_int(buffer_id)
        mid = _as_int(msg_id)
        if bid is not None and mid is not None:
            self.marker_lines_by_buffer[bid] = mid

    @sync_slot(b"removeBuffer")
    def _sync_remove_buffer(self, buffer_id: Any) -> None:
        bid = _as_int(buffer_id)
        if bid is not None:
            self.removed_buffers.add(bid)
            self.last_seen_by_buffer.pop(bid, None)
            self.marker_lines_by_buffer.pop(bid, None)

    @sync_slot(b"renameBuffer")
    def _sync_rename_buffer(self, buffer_id: Any, name: Any) -> None:
        """Record a pending buffer rename for the dispatcher to apply.

        We don't own `BufferInfo.name` — that lives on `ClientState.buffers`
        — so the rename lands in `renamed_buffers` here and the dispatcher
        drains it in `_emit_slot_side_effects`, updating the canonical
        BufferInfo and emitting a public `BufferRenamed` event.
        """
        bid = _as_int(buffer_id)
        if bid is not None and name is not None:
            self.renamed_buffers[bid] = str(name)

    @sync_slot(b"mergeBuffersPermanently")
    def _sync_merge_buffers(self, buffer_id1: Any, buffer_id2: Any) -> None:
        # The second buffer is merged into the first; only buffer_id1 survives.
        bid2 = _as_int(buffer_id2)
        if bid2 is not None:
            self.removed_buffers.add(bid2)
            self.last_seen_by_buffer.pop(bid2, None)
            self.marker_lines_by_buffer.pop(bid2, None)

    @sync_slot(b"markBufferAsRead")
    def _sync_mark_buffer_as_read(self, _buffer_id: Any) -> None:
        # Core-side only operation for v1 — same as renameBuffer, we just
        # acknowledge the slot exists so the base class doesn't DEBUG-log it.
        return

    # -- init-field handlers ------------------------------------------------

    @init_field("LastSeenMsg")
    def _init_last_seen(self, value: Any) -> None:
        """Initial last-seen map: `{bufferId: msgId}` as a `QVariantMap`.

        Quassel emits this as a map whose keys are `BufferId` user-type
        instances (or stringified ints on older cores). We normalize both
        keys and values to plain ints so downstream code doesn't have to
        unpack variant wrappers on every lookup.
        """
        for key, val in _iter_pairs(value):
            bid = _as_int(key)
            mid = _as_int(val)
            if bid is not None and mid is not None:
                self.last_seen_by_buffer[bid] = mid

    @init_field("MarkerLines")
    def _init_marker_lines(self, value: Any) -> None:
        for key, val in _iter_pairs(value):
            bid = _as_int(key)
            mid = _as_int(val)
            if bid is not None and mid is not None:
                self.marker_lines_by_buffer[bid] = mid


def _iter_pairs(value: Any) -> list[tuple[Any, Any]]:
    """Normalize a QVariantMap / QVariantList-of-pairs into (key, value) tuples.

    Older Quassel cores serialize the `{bufferId: msgId}` map as a flat
    alternating list of `[BufferId, MsgId, BufferId, MsgId, ...]`; modern
    cores use a real QVariantMap. We accept either shape because the
    captured byte fixtures span both and we'd rather degrade gracefully.
    """
    if isinstance(value, dict):
        return list(value.items())
    if isinstance(value, list) and len(value) % 2 == 0:
        pairs: list[tuple[Any, Any]] = []
        for i in range(0, len(value), 2):
            pairs.append((value[i], value[i + 1]))
        return pairs
    return []


def _as_int(value: Any) -> int | None:
    """Coerce a BufferId/MsgId/int into a plain Python int, or None."""
    if isinstance(value, BufferId | MsgId):
        return int(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BufferSyncer",
]
