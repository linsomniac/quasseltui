"""`ClientState` — the canonical store of what the client knows.

Everything the UI renders reads from this. The dispatcher is the only
writer: a single task runs the protocol connection, drives the dispatcher,
and every mutation happens inside that one task. There is no locking
because there is no concurrency across tasks — callers that want to read
from a different task should use the event stream to observe changes and
then re-read.

What lives here:

- `networks`: `dict[NetworkId, Network]` — the live SyncObject instances.
- `buffers`: `dict[BufferId, BufferInfo]` — canonical buffer metadata.
  Buffers are not syncable; the dispatcher mutates this dict directly.
- `messages`: `dict[BufferId, list[IrcMessage]]` — per-buffer history.
  Newest at the end (append-only in phase 5; phase 10 adds prepend for
  backlog).
- `identities`: `dict[IdentityId, Identity]` — the Identity SyncObjects.
- `buffer_syncer`: the singleton `BufferSyncer`, or `None` before the
  dispatcher has created it.
- `session` / `peer_features`: echoes of the handshake result for
  anything downstream that wants to reason about feature flags.

What does NOT live here: the protocol connection itself, the dispatcher,
or any task-management state. Those belong to `QuasselClient`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from quasseltui.protocol.messages import SessionInit
from quasseltui.protocol.usertypes import BufferId, BufferInfo, IdentityId, NetworkId
from quasseltui.sync.buffer_syncer import BufferSyncer
from quasseltui.sync.events import IrcMessage
from quasseltui.sync.identity import Identity
from quasseltui.sync.network import Network


@dataclass
class ClientState:
    """Canonical view of the current Quassel session, single-writer.

    `max_messages_per_buffer` is the retention cap the dispatcher enforces
    when a new message arrives via `displayMsg`. Without a cap, a busy
    channel or a malicious peer could inflate memory unbounded over a
    long-lived session — this is a practical resource-DoS vector because
    IRC traffic is untrusted input. The default of 5000 is large enough
    to never bite a typical user (a flood of 5000 messages is ~a day on
    #python) but bounds the worst case. Set to 0 to disable the cap
    entirely; only do that in tests where you control the input volume.
    """

    session: SessionInit | None = None
    peer_features: frozenset[str] = field(default_factory=frozenset)
    networks: dict[NetworkId, Network] = field(default_factory=dict)
    buffers: dict[BufferId, BufferInfo] = field(default_factory=dict)
    messages: dict[BufferId, list[IrcMessage]] = field(default_factory=dict)
    identities: dict[IdentityId, Identity] = field(default_factory=dict)
    buffer_syncer: BufferSyncer | None = None
    max_messages_per_buffer: int = 5000

    def network_for_buffer(self, buffer_id: BufferId) -> Network | None:
        """Convenience: find the `Network` a buffer belongs to, or `None`.

        Used by the CLI dump-state command and eventually the TUI status
        bar. Returns `None` rather than raising if the buffer isn't in the
        map — the buffer may have been removed by the core after we
        emitted the `BufferRemoved` event but before the caller read.
        """
        info = self.buffers.get(buffer_id)
        if info is None:
            return None
        return self.networks.get(info.network_id)

    def messages_for_buffer(self, buffer_id: BufferId) -> list[IrcMessage]:
        """Return the live list for a buffer, creating an empty one if needed.

        This is intentionally the live list — mutating it mutates state.
        We expose it this way because the UI iterates in-place for
        rendering and making a defensive copy on every lookup would
        dominate the render path on a busy channel.
        """
        return self.messages.setdefault(buffer_id, [])

    def total_message_count(self) -> int:
        return sum(len(msgs) for msgs in self.messages.values())


__all__ = [
    "ClientState",
]
