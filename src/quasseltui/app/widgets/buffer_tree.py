"""Left-pane buffer navigator.

Renders `ClientState.networks` + `ClientState.buffers` as a two-level tree:

```
├── Libera.Chat
│   ├── (status)
│   ├── #python
│   ├── #rust
│   └── nickbot
└── OFTC
    ├── (status)
    └── #debian
```

Each leaf's `data` attribute carries the `BufferInfo` that phase 8 will
use to drive the active-buffer selection. Network-level nodes carry
`None` as `data`, so downstream code can cheaply discriminate
"user clicked a network header" vs "user clicked a buffer".

Phase 6 responsibility: render the tree once on mount. No interactivity
— arrow-key navigation + click → `BufferSelected` message lands in
phase 8.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Tree

from quasseltui.client.state import ClientState
from quasseltui.protocol.usertypes import BufferInfo, BufferType, NetworkId
from quasseltui.util.text import sanitize_terminal


class BufferTree(Tree[BufferInfo | None]):
    """Two-level tree: networks at the top, buffers as leaves."""

    def __init__(self, state: ClientState, *, id: str | None = None) -> None:
        # The root node label is hidden via `show_root=False` below, so
        # the value here only matters if a future debug overlay reveals it.
        super().__init__(label="networks", id=id)
        self._state = state
        self.show_root = False

    def on_mount(self) -> None:
        self._populate()

    def _populate(self) -> None:
        """Walk the client state once and build the tree.

        We sort networks by id (stable, matches dump-state output) and
        buffers within a network by (type, name) so status buffers rise
        above channels and channels rise above queries. Lowercase the
        name for the sort key — real Quassel users mix `#Python` and
        `#python` capitalisation and the visual order should not flip.

        Labels are always passed as `rich.text.Text` instances rather
        than raw strings. Textual's `Tree.process_label` converts a
        `str` label via `Text.from_markup`, which means a channel or
        network name like `"[red]spoof[/]"` would get parsed as Rich
        markup and restyled — a remote-controlled cosmetic spoof. A
        pre-built `Text` bypasses `from_markup` entirely and is
        rendered literally.
        """
        for network_id in sorted(self._state.networks, key=int):
            network = self._state.networks[network_id]
            raw_label = network.network_name or f"(network {int(network_id)})"
            node = self.root.add(_safe_label(raw_label), data=None, expand=True)
            buffers = [
                buf
                for buf in self._state.buffers.values()
                if buf.network_id == NetworkId(int(network_id))
            ]
            buffers.sort(key=_buffer_sort_key)
            for buf in buffers:
                node.add_leaf(_safe_label(_buffer_label(buf)), data=buf)


def _safe_label(raw: str) -> Text:
    """Build a `Text` suitable for passing to Textual's `Tree.add`.

    Combines the terminal-safety step (escape C0/C1 controls) with the
    markup-safety step (return a `Text` so Textual does not run
    `Text.from_markup` over the string). Kept as a single helper so
    every sidebar call site gets both guarantees without having to
    remember two steps.
    """
    return Text(sanitize_terminal(raw))


def _buffer_sort_key(buf: BufferInfo) -> tuple[int, str]:
    return (buf.type.value, buf.name.lower())


def _buffer_label(buf: BufferInfo) -> str:
    """Human-readable sidebar label for a buffer.

    Status buffers have an empty name on the wire; show a stable
    placeholder so the user sees something selectable.
    """
    if buf.type == BufferType.Status:
        return "(status)"
    return buf.name or "(unnamed)"


__all__ = [
    "BufferTree",
]
