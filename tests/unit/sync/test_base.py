"""Unit tests for `quasseltui.sync.base` — SyncObject + decorators.

The base class is tiny but load-bearing: every SyncObject subclass relies on
`__init_subclass__` to walk the MRO and pull `@sync_slot` / `@init_field`
markers into per-class lookup dicts. These tests pin:

- decorator registration into `_sync_slots` / `_init_fields`
- dispatch by slot name with positional params
- log-and-drop for unknown slots and unknown init fields
- log-and-drop for slot handlers that raise TypeError / ValueError
- MRO ordering — a subclass can override an inherited slot by re-declaring
  it with the same name

If any of these change shape, every SyncObject subclass needs updating —
which is exactly why the pin lives here at the base-class level.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from quasseltui.sync.base import SyncObject, init_field, sync_slot


class _FakeObject(SyncObject):
    CLASS_NAME = b"Fake"

    def __init__(self, object_name: str = "") -> None:
        super().__init__(object_name)
        self.name = ""
        self.count = 0
        self.tags: list[str] = []

    @sync_slot(b"setName")
    def _sync_set_name(self, name: str) -> None:
        self.name = name

    @sync_slot("incrementBy")
    def _sync_increment_by(self, n: int) -> None:
        self.count += int(n)

    @sync_slot(b"addTag")
    def _sync_add_tag(self, tag: str) -> None:
        self.tags.append(tag)

    @init_field("name")
    def _init_name(self, value: Any) -> None:
        self.name = str(value)

    @init_field("tags")
    def _init_tags(self, value: Any) -> None:
        if isinstance(value, list):
            self.tags = [str(t) for t in value]


class TestSlotRegistration:
    def test_sync_slots_registered_by_name(self) -> None:
        assert b"setName" in _FakeObject._sync_slots
        assert b"incrementBy" in _FakeObject._sync_slots
        assert b"addTag" in _FakeObject._sync_slots

    def test_init_fields_registered_by_name(self) -> None:
        assert "name" in _FakeObject._init_fields
        assert "tags" in _FakeObject._init_fields

    def test_string_slot_name_is_encoded_to_bytes(self) -> None:
        # `incrementBy` is declared with a str literal in the decorator —
        # it must still land in the registry as bytes so dispatch by
        # SyncMessage.slot_name (which is always bytes) works.
        assert b"incrementBy" in _FakeObject._sync_slots
        assert "incrementBy" not in _FakeObject._sync_slots  # type: ignore[comparison-overlap]

    def test_base_class_has_empty_registries(self) -> None:
        # SyncObject itself should have no slots; subclasses build their
        # own via `__init_subclass__`.
        assert SyncObject._sync_slots == {}
        assert SyncObject._init_fields == {}


class TestSlotDispatch:
    def test_sync_call_invokes_registered_handler(self) -> None:
        obj = _FakeObject()
        obj.handle_sync(b"setName", ["freenode"])
        assert obj.name == "freenode"

    def test_multiple_params_unpack_positionally(self) -> None:
        obj = _FakeObject()
        obj.handle_sync(b"incrementBy", [5])
        obj.handle_sync(b"incrementBy", [10])
        assert obj.count == 15

    def test_unknown_slot_is_logged_and_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        obj = _FakeObject()
        with caplog.at_level(logging.DEBUG, logger="quasseltui.sync.base"):
            obj.handle_sync(b"nonExistentSlot", ["ignored"])
        assert any("unknown slot" in r.getMessage() for r in caplog.records)
        # And nothing on the object was touched.
        assert obj.name == ""
        assert obj.count == 0

    def test_slot_type_error_is_caught_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        obj = _FakeObject()
        with caplog.at_level(logging.WARNING, logger="quasseltui.sync.base"):
            # Wrong arity: setName expects 1 arg, we're passing 0.
            obj.handle_sync(b"setName", [])
        # No exception escaped; a warning was logged.
        assert any("raised TypeError" in r.getMessage() for r in caplog.records)

    def test_slot_value_error_is_caught_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        obj = _FakeObject()
        with caplog.at_level(logging.WARNING, logger="quasseltui.sync.base"):
            # incrementBy calls int() on its argument; passing a non-numeric
            # str raises ValueError which handle_sync is supposed to catch.
            obj.handle_sync(b"incrementBy", ["not a number"])
        assert any("raised ValueError" in r.getMessage() for r in caplog.records)
        assert obj.count == 0  # handler was rolled back by exception


class TestInitFieldDispatch:
    def test_apply_init_data_fills_registered_fields(self) -> None:
        obj = _FakeObject()
        obj.apply_init_data({"name": "netA", "tags": ["foo", "bar"]})
        assert obj.name == "netA"
        assert obj.tags == ["foo", "bar"]
        assert obj.initialized is True

    def test_unknown_field_is_logged_and_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        obj = _FakeObject()
        with caplog.at_level(logging.DEBUG, logger="quasseltui.sync.base"):
            obj.apply_init_data({"name": "ok", "whatNow": 123})
        assert obj.name == "ok"  # registered field was applied
        assert any("unknown init field" in r.getMessage() for r in caplog.records)
        assert obj.initialized is True  # completion flag still flips

    def test_empty_init_data_still_marks_initialized(self) -> None:
        obj = _FakeObject()
        assert obj.initialized is False
        obj.apply_init_data({})
        assert obj.initialized is True


class TestSubclassOverride:
    """A subclass can re-declare a slot name to override the base handler."""

    def test_override_replaces_base_handler(self) -> None:
        class Child(_FakeObject):
            @sync_slot(b"setName")
            def _child_set_name(self, name: str) -> None:
                self.name = f"child:{name}"

        child = Child()
        child.handle_sync(b"setName", ["hello"])
        assert child.name == "child:hello"

        # The parent's registry is untouched.
        parent = _FakeObject()
        parent.handle_sync(b"setName", ["hello"])
        assert parent.name == "hello"

    def test_inherits_unshadowed_slots(self) -> None:
        class Child(_FakeObject):
            pass

        child = Child()
        child.handle_sync(b"addTag", ["foo"])
        assert child.tags == ["foo"]
