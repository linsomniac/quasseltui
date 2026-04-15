"""L4 — Embeddable QuasselClient + ClientState + public Event types.

Public surface:

- `QuasselClient` — the one class embedders construct. Drives the
  protocol connection, owns a `ClientState`, yields `ClientEvent`s.
- `ClientState` — the canonical read-only store.
- `quasseltui.client.events` — public re-exports of the sync-layer event
  dataclasses (`SessionOpened`, `BufferAdded`, `MessageReceived`,
  `Disconnected`, ...).

Everything else (`Dispatcher`, `SyncObject`, per-class SyncObjects) is
internal — there's no stability guarantee for those names, and they're
subject to change as phase 6+ fleshes out the UI.
"""

from quasseltui.client.client import QuasselClient
from quasseltui.client.state import ClientState

__all__ = [
    "ClientState",
    "QuasselClient",
]
