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

Each leaf's `data` attribute carries the `BufferInfo` that drives the
active-buffer selection. Network-level nodes carry `None` as `data`, so
downstream code can cheaply discriminate "user clicked a network header"
vs "user clicked a buffer".

Interactivity (phase 8): `on_tree_node_selected` posts a `BufferSelected`
message for every leaf selection — click, Enter, or a programmatic
`select_node` call from the app's alt+up/alt+down cycle bindings. The
app is the single authority for `active_buffer_id`; this widget never
mutates app state directly.

Cursor preservation: `refresh_from_state()` can fire at any moment (a
channel join, a buffer rename, a network disconnect). We remember the
last buffer the app told us was active in `_active_hint` and re-seek
to it after rebuilding so a sidebar refresh doesn't dump the user back
to the top of the list. The app also calls `set_active_buffer()` when
alt+up/down moves the cursor programmatically, keeping the sidebar
visual in sync with the active buffer pointer.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from quasseltui.app.messages import BufferSelected
from quasseltui.client.state import ClientState
from quasseltui.protocol.usertypes import BufferId, BufferInfo, BufferType, NetworkId
from quasseltui.util.text import sanitize_terminal


class BufferTree(Tree[BufferInfo | None]):
    """Two-level tree: networks at the top, buffers as leaves."""

    def __init__(self, state: ClientState, *, id: str | None = None) -> None:
        # The root node label is hidden via `show_root=False` below, so
        # the value here only matters if a future debug overlay reveals it.
        super().__init__(label="networks", id=id)
        self._state = state
        self.show_root = False
        # `_active_hint` is the buffer_id the app most recently told us
        # is active. Used by `refresh_from_state` to re-seek the cursor
        # after a rebuild, so a sidebar refresh caused by a live event
        # doesn't lose the user's place.
        self._active_hint: BufferId | None = None

    def on_mount(self) -> None:
        self._populate()

    def refresh_from_state(self) -> None:
        """Clear the tree and rebuild it from the current state.

        Called by `QuasselApp._on_buffer_list_updated` whenever the
        bridge signals that the sidebar needs a redraw (network or
        buffer added / removed / renamed). `Tree.clear()` replaces
        the root node wholesale (preserving label, data, and
        expanded flag) and `_populate()` then rebuilds the children
        from scratch, so a stale hierarchy cannot survive the call.

        If the app has told us which buffer is active, we re-seek
        the cursor to it after the rebuild. Without this, a live
        `BufferAdded` during an active session would bounce the
        cursor back to the first leaf and confuse the user.
        """
        self.clear()
        self._populate()
        if self._active_hint is not None:
            self._select_leaf_for_buffer(self._active_hint)

    def set_active_buffer(self, buffer_id: BufferId) -> None:
        """Remember the app-side active buffer and move the cursor to it.

        Called from `QuasselApp._set_active_buffer` after the alt+up /
        alt+down cycle bindings flip `active_buffer_id`. Calling
        `select_node` posts a `Tree.NodeSelected` that our own handler
        forwards as `BufferSelected` — that round-trips back to the
        app, which early-returns because `active_buffer_id` already
        matches. One idempotent round-trip, not a loop.
        """
        self._active_hint = buffer_id
        self._select_leaf_for_buffer(buffer_id)

    def on_tree_node_selected(self, event: Tree.NodeSelected[BufferInfo | None]) -> None:
        """Forward leaf selections as `BufferSelected` messages.

        Fires on click, Enter, and programmatic `select_node` calls.
        Network-header nodes carry `None` as data — we ignore them so
        a stray click on a header doesn't try to switch to a
        non-existent buffer. We also stop the original message from
        bubbling further up the DOM; the app subscribes to our
        `BufferSelected` instead, which keeps the contract narrow.
        """
        event.stop()
        buf = event.node.data
        if buf is None:
            return
        self.post_message(BufferSelected(buffer_id=buf.buffer_id))

    def _select_leaf_for_buffer(self, buffer_id: BufferId) -> None:
        """Move the tree cursor to the leaf for `buffer_id`, if present.

        Silently no-ops if the buffer is not in the tree yet — this
        can happen during a race where the app flips the active
        pointer before the bridge has emitted the `BufferAdded` that
        would cause us to include the leaf. The next
        `refresh_from_state` will fix it via the `_active_hint`
        re-seek path.
        """
        leaf = self._find_leaf_for_buffer(buffer_id)
        if leaf is not None:
            self.select_node(leaf)

    def _find_leaf_for_buffer(self, buffer_id: BufferId) -> TreeNode[BufferInfo | None] | None:
        for network_node in self.root.children:
            for leaf in network_node.children:
                data = leaf.data
                if data is not None and data.buffer_id == buffer_id:
                    return leaf
        return None

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
