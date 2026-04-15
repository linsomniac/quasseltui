"""Center-pane scrollback for the active buffer.

Phase 6: renders `state.messages[active_buffer_id]` once at mount time
into a `RichLog`. The message formatter lives here so phase 7 can reuse
it when handling live `MessageReceived` events.

Phase 7 will add `append_message()` that the bridge calls on each live
event. Phase 8 will add `set_active_buffer()` that clears and re-reads
when the user switches. Both of those are deliberately left unwritten
for now so the diff stays reviewable one phase at a time.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import RichLog

from quasseltui.client.state import ClientState
from quasseltui.protocol.enums import MessageType
from quasseltui.protocol.usertypes import BufferId
from quasseltui.sync.events import IrcMessage
from quasseltui.util.text import sanitize_terminal

_TYPE_PREFIX: dict[MessageType, str] = {
    MessageType.Plain: "",
    MessageType.Notice: "NOTICE ",
    MessageType.Action: "* ",
    MessageType.Join: "--> ",
    MessageType.Part: "<-- ",
    MessageType.Quit: "<-- ",
    MessageType.Kick: "<-- ",
    MessageType.Nick: "-- ",
    MessageType.Mode: "-- ",
    MessageType.Topic: "-- ",
    MessageType.Server: "!! ",
    MessageType.Error: "!! ",
    MessageType.Info: "-- ",
}


class MessageLog(RichLog):
    """Scrollback view — one line per message, formatted for the terminal."""

    def __init__(self, state: ClientState, *, id: str | None = None) -> None:
        super().__init__(id=id, wrap=True, markup=False, highlight=False, auto_scroll=True)
        self._state = state
        self._active_buffer: BufferId | None = None

    def on_mount(self) -> None:
        active = self._pick_initial_buffer()
        if active is not None:
            self.set_active_buffer(active)

    def set_active_buffer(self, buffer_id: BufferId) -> None:
        """Replace the log contents with the given buffer's history.

        Not called by any user-facing code in phase 6 — the initial
        `on_mount` hook picks a buffer and that's the only switch until
        phase 8 wires up the tree. Exposing it here early means phase 8
        is a one-line change in the `BufferSelected` handler.
        """
        self._active_buffer = buffer_id
        self.clear()
        for msg in self._state.messages.get(buffer_id, []):
            self.write(format_message(msg))

    def _pick_initial_buffer(self) -> BufferId | None:
        """Default-select the first buffer that has any history.

        Phase 6 has no persisted "last active buffer" concept — a real
        quasselclient would restore the previously-opened buffer from
        `BufferViewConfig`, which we defer to phase 11. For the demo we
        just want to show *something* when the app opens, so we prefer
        a buffer with messages; fall back to the first buffer of any
        kind if nothing has content yet.
        """
        for buffer_id, messages in self._state.messages.items():
            if messages:
                return buffer_id
        return next(iter(self._state.buffers), None)


def format_message(msg: IrcMessage) -> Text:
    """Render one `IrcMessage` as a `rich.text.Text` line.

    Shape: `HH:MM:SS prefix<sender> contents` for Plain/Notice, with a
    type-specific prefix for the non-chat event types. The output is a
    `Text` (not a raw str) so phase 11 can colour senders via
    `Text.stylize` without rewriting the formatter.

    Every user- or core-provided string (sender, sender_prefixes,
    contents) is passed through `sanitize_terminal` first. A
    `rich.text.Text` built from a plain string does NOT strip embedded
    ANSI/C0/C1 bytes at render time — the bytes pass straight through
    to the terminal driver — so the onus is on us to remove them here.
    The type prefix and timestamp are compile-time ASCII and do not
    need sanitizing.
    """
    ts = msg.timestamp.astimezone().strftime("%H:%M:%S")
    prefix = _TYPE_PREFIX.get(msg.type, "")
    sender = sanitize_terminal(_short_sender(msg.sender))
    contents = sanitize_terminal(msg.contents)
    if msg.type == MessageType.Action:
        return Text(f"{ts} {prefix}{sender} {contents}")
    sender_prefixes = sanitize_terminal(msg.sender_prefixes)
    return Text(f"{ts} {prefix}{sender_prefixes}{sender}: {contents}")


def _short_sender(sender: str) -> str:
    """Strip the `!user@host` off an IRC hostmask, if present.

    Quassel ships `displayMsg` senders as full hostmasks; we keep the
    nick portion only for the UI, leaving the full string in
    `state.messages` for anything that needs to reason about user@host.
    """
    return sender.split("!", 1)[0] if "!" in sender else sender


__all__ = [
    "MessageLog",
    "format_message",
]
