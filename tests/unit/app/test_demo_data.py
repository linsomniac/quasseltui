"""Unit tests for `quasseltui.app.demo_data.build_demo_state`.

The demo state is the single input the phase 6 UI renders from, so
these tests pin the invariants the widgets assume — mainly that the
shapes match what a real `ClientState` would carry after a handshake:

- Networks exist and have human-visible names / nicks.
- Every buffer has a pre-allocated (possibly empty) message list so
  the message log can iterate without special-casing "first message".
- At least one buffer has messages so the `MessageLog` on-mount
  picker has something non-empty to select.
- Two successive builds are independent — no mutable default trap.
"""

from __future__ import annotations

from quasseltui.app.demo_data import build_demo_state
from quasseltui.protocol.usertypes import BufferId, BufferType, NetworkId


class TestBuildDemoState:
    def test_networks_populated_with_nicks(self) -> None:
        state = build_demo_state()
        assert set(state.networks) == {NetworkId(1), NetworkId(2)}
        libera = state.networks[NetworkId(1)]
        assert libera.network_name == "Libera.Chat"
        assert libera.my_nick == "seanr"

    def test_every_buffer_has_a_message_list(self) -> None:
        state = build_demo_state()
        assert set(state.buffers) == {
            BufferId(10),
            BufferId(11),
            BufferId(12),
            BufferId(13),
            BufferId(20),
            BufferId(21),
        }
        for buffer_id in state.buffers:
            assert buffer_id in state.messages

    def test_at_least_one_buffer_has_content(self) -> None:
        state = build_demo_state()
        with_content = [bid for bid, msgs in state.messages.items() if msgs]
        assert with_content, "MessageLog on_mount expects at least one non-empty buffer"

    def test_buffer_types_span_the_visible_categories(self) -> None:
        state = build_demo_state()
        types = {buf.type for buf in state.buffers.values()}
        assert BufferType.Status in types
        assert BufferType.Channel in types
        assert BufferType.Query in types

    def test_successive_builds_are_independent(self) -> None:
        first = build_demo_state()
        second = build_demo_state()
        assert first is not second
        assert first.networks is not second.networks
        assert first.buffers is not second.buffers
        # Mutating one must not reach into the other — guards against
        # a demo_data refactor accidentally sharing a module-level dict.
        first.networks[NetworkId(1)].network_name = "mutated"
        assert second.networks[NetworkId(1)].network_name == "Libera.Chat"
