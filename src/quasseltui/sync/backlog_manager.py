"""BacklogManager — handles backlog responses from the core.

The Quassel core's `BacklogManager` is a global singleton ("" object
name) that the client drives with `requestBacklog(BufferId, MsgId,
MsgId, int, int)` sync calls. The core responds by calling
`receiveBacklog(BufferId, MsgId, MsgId, int, int, QVariantList)` back
on *our* side, where the trailing `QVariantList` is a list of `Message`
user-type values — the historical IRC messages for the requested range.

We don't need InitData for this object (it has no persistent state on
the core side), so no `@init_field` handlers are registered. The
dispatcher creates the instance during `seed_from_session` and
registers it so inbound `receiveBacklog` Sync frames get routed here.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from quasseltui.protocol.usertypes import Message
from quasseltui.sync.base import SyncObject, sync_slot

_log = logging.getLogger(__name__)


class BacklogManager(SyncObject):
    """Receive-side of the Quassel backlog request/response cycle."""

    CLASS_NAME: ClassVar[bytes] = b"BacklogManager"

    def __init__(self, object_name: str = "") -> None:
        super().__init__(object_name)
        self.last_received: list[Message] = []

    # AIDEV-NOTE: The core sends receiveBacklog as a Sync message with
    # 6 params: (BufferId, MsgId first, MsgId last, int limit,
    # int additional, QVariantList messages). The QVariantList is a
    # Python list of Message user-type instances by the time the
    # variant decoder hands it to us. We store the raw Messages and
    # let the dispatcher's post-sync hook convert them to IrcMessages
    # and merge them into state.
    @sync_slot(b"receiveBacklog")
    def _sync_receive_backlog(
        self,
        buffer_id: Any,
        first: Any,
        last: Any,
        limit: Any,
        additional: Any,
        messages: Any,
    ) -> None:
        if not isinstance(messages, list):
            _log.warning(
                "receiveBacklog: expected list of Messages, got %s",
                type(messages).__name__,
            )
            return
        self.last_received = [m for m in messages if isinstance(m, Message)]
        _log.debug(
            "receiveBacklog for buffer %s: %d messages",
            buffer_id,
            len(self.last_received),
        )


__all__ = [
    "BacklogManager",
]
