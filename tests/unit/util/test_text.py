"""Unit tests for `quasseltui.util.text.sanitize_terminal`.

This module is the single source of truth for control-character
escaping across the CLI (`dump-state`), the Textual widgets
(`message_log`, `buffer_tree`), and anything else that eventually
prints untrusted IRC text. Pinning the exact input/output for a small
but representative set of payloads guards against:

- a careless regex tweak silently letting ESC through,
- the replacement format drifting away from `\\xNN` and breaking the
  debuggability contract,
- idempotency regressions (the second application must be a no-op).
"""

from __future__ import annotations

from quasseltui.util.text import sanitize_terminal


class TestSanitizeTerminal:
    def test_plain_ascii_passes_through(self) -> None:
        assert sanitize_terminal("hello world") == "hello world"

    def test_utf8_passes_through(self) -> None:
        assert sanitize_terminal("résumé") == "résumé"

    def test_irc_channel_name_passes_through(self) -> None:
        assert sanitize_terminal("#python-dev") == "#python-dev"

    def test_escape_is_replaced_with_backslash_hex(self) -> None:
        cleaned = sanitize_terminal("\x1b[31mRED")
        assert "\x1b" not in cleaned
        assert "\\x1b" in cleaned

    def test_bel_backspace_cr_lf_tab(self) -> None:
        # All five of these are C0 controls that a malicious peer can
        # use to rewrite or spoof preceding terminal output.
        cleaned = sanitize_terminal("a\x07b\x08c\x0dd\x0ae\x09f")
        for raw in ("\x07", "\x08", "\x0d", "\x0a", "\x09"):
            assert raw not in cleaned

    def test_nul_and_del_are_replaced(self) -> None:
        cleaned = sanitize_terminal("a\x00b\x7fc")
        assert "\x00" not in cleaned
        assert "\x7f" not in cleaned
        assert "\\x00" in cleaned
        assert "\\x7f" in cleaned

    def test_c1_control_range(self) -> None:
        # 0x80-0x9f is the 8-bit CSI range some terminals still honor.
        cleaned = sanitize_terminal("a\x9bb\x9cc")
        assert "\x9b" not in cleaned
        assert "\x9c" not in cleaned

    def test_idempotent_on_already_sanitized_input(self) -> None:
        once = sanitize_terminal("\x1b[0m")
        twice = sanitize_terminal(once)
        assert once == twice
        assert "\x1b" not in twice

    def test_empty_string(self) -> None:
        assert sanitize_terminal("") == ""

    def test_rich_markup_is_left_alone(self) -> None:
        # The sanitizer is NOT responsible for stripping Rich markup —
        # that's the caller's job (pass a `Text(...)` to Textual). We
        # pin this behavior so a future "also escape [" regex change
        # is a deliberate choice, not a surprise.
        raw = "[bold red]spoof[/]"
        assert sanitize_terminal(raw) == raw
