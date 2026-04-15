"""SyncObject base + decorator-registered slot and init-field handlers.

A `SyncObject` corresponds to one C++ Quassel class that participates in the
SignalProxy mesh — `Network`, `IrcChannel`, `IrcUser`, `Identity`,
`BufferSyncer`, etc. Each instance is identified on the wire by
`(className, objectName)` where the objectName is a per-class string key:
`"1"` for network id 1, `"1/#python"` for the channel #python on network 1,
`""` for a singleton like `BufferSyncer`.

Subclasses register handlers with two module-scope decorators:

    class Network(SyncObject):
        CLASS_NAME = b"Network"

        @sync_slot(b"setNetworkName")
        def _sync_set_network_name(self, name: str) -> None:
            self.network_name = name

        @init_field("networkName")
        def _init_network_name(self, value: object) -> None:
            self.network_name = str(value)

At class-definition time `__init_subclass__` walks the subclass's MRO and
pulls every decorated method into two per-class dicts. At dispatch time the
base class looks the name up, and calls the function with `self` plus the
positional args. Unknown slot names and unknown init-field keys are logged
at DEBUG and dropped — the whole point of a forward-compatible wire format
is that a newer core can add slots we don't understand without breaking the
connection.

**Why two registries?** On the wire, slots and init fields are separate
things:

- A `Sync(className, objectName, slotName, ...params)` frame invokes a
  named slot with positional params. State *changes* flow through here.
- An `InitData(className, objectName, {prop: value, ...})` frame hands us a
  flat property map. Current *state* arrives through here.

Conceptually a C++ SyncObject defines both as Qt meta-objects; we model them
as two explicit decorators so tests can round-trip each in isolation and so a
grep for `@sync_slot` gives an accurate picture of what we respond to.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, ClassVar, TypeVar

_log = logging.getLogger(__name__)


# `sync_slot` and `init_field` are dunder-suffix markers so `__init_subclass__`
# can find them without importing a separate registry module. Keeping the
# attribute name tightly namespaced (double underscore, module-private prefix)
# means a subclass can't accidentally trample it.
_SYNC_SLOT_ATTR = "__quasseltui_sync_slot_name__"
_INIT_FIELD_ATTR = "__quasseltui_init_field_key__"


F = TypeVar("F", bound=Callable[..., Any])


def sync_slot(name: str | bytes) -> Callable[[F], F]:
    """Mark a method as a handler for the SignalProxy slot `name`.

    `name` is the raw slot name from the C++ source (e.g. `"setNetworkName"`,
    `"joinIrcUsers"`). Passed as either `str` or `bytes`; stored internally
    as bytes because the wire format is a `QByteArray` and the dispatcher
    compares against `SyncMessage.slot_name` which is `bytes`.
    """
    raw = name.encode("ascii") if isinstance(name, str) else bytes(name)

    def decorator(fn: F) -> F:
        setattr(fn, _SYNC_SLOT_ATTR, raw)
        return fn

    return decorator


def init_field(name: str) -> Callable[[F], F]:
    """Mark a method as the handler for an `InitData` property named `name`.

    `name` is the exact key string from the core's init map — Quassel tends
    to camelCase these (`networkName`, `isConnected`, `ircUsersAndChannels`)
    so we keep them as `str` (no ASCII encoding) to avoid quiet mismatches.
    """

    def decorator(fn: F) -> F:
        setattr(fn, _INIT_FIELD_ATTR, name)
        return fn

    return decorator


class SyncObject:
    """Base for every `sync/*.py` model class.

    Subclasses MUST set `CLASS_NAME` to the exact C++ class name the core
    uses (as a `bytes` literal, since that's what travels in SignalProxy
    frames). The base class itself has `CLASS_NAME = b""`; the dispatcher
    checks for the empty default and refuses to register a subclass that
    forgot to override it.
    """

    CLASS_NAME: ClassVar[bytes] = b""

    # Populated per-subclass by `__init_subclass__`. Kept as a plain dict
    # rather than a `MappingProxy` so tests can introspect and (if they
    # really need to) register additional handlers on a subclass created
    # just for the test.
    _sync_slots: ClassVar[dict[bytes, Callable[..., None]]] = {}
    _init_fields: ClassVar[dict[str, Callable[..., None]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Walk the MRO in reverse so base-class registrations land first and
        # subclasses can override them by re-declaring the same slot name.
        slots: dict[bytes, Callable[..., None]] = {}
        fields: dict[str, Callable[..., None]] = {}
        for klass in reversed(cls.__mro__):
            for attr in vars(klass).values():
                slot_name = getattr(attr, _SYNC_SLOT_ATTR, None)
                if isinstance(slot_name, bytes):
                    slots[slot_name] = attr
                field_name = getattr(attr, _INIT_FIELD_ATTR, None)
                if isinstance(field_name, str):
                    fields[field_name] = attr
        cls._sync_slots = slots
        cls._init_fields = fields

    def __init__(self, object_name: str) -> None:
        self.object_name = object_name
        # `initialized` flips to True after `apply_init_data` finishes so
        # downstream code can tell "seed data hasn't arrived yet" apart from
        # "the object exists but is empty" during the InitRequest fan-out.
        self.initialized: bool = False

    # -- dispatch entry points used by the dispatcher -----------------------

    def handle_sync(self, slot_name: bytes, params: list[Any]) -> None:
        """Dispatch a Sync call to the matching `@sync_slot` method.

        Unknown slot names are logged at DEBUG and dropped. A handler that
        raises `TypeError` or `ValueError` (e.g. because the core sent a
        wrong number of args or an unexpected type) is caught, logged at
        WARNING, and dropped — the CONNECTED-state read loop can't
        meaningfully recover from one slot failing, and bringing down the
        whole connection over a single malformed slot would be worse.
        """
        handler = type(self)._sync_slots.get(slot_name)
        if handler is None:
            _log.debug(
                "%s(%r): unknown slot %r, dropping %d params",
                type(self).__name__,
                self.object_name,
                slot_name,
                len(params),
            )
            return
        try:
            handler(self, *params)
        except (TypeError, ValueError) as exc:
            _log.warning(
                "%s(%r): slot %r raised %s: %s",
                type(self).__name__,
                self.object_name,
                slot_name,
                type(exc).__name__,
                exc,
            )

    def apply_init_data(self, init_data: dict[str, Any]) -> None:
        """Apply one `InitData` property map to this instance.

        Each key is routed through `apply_init_field`; after every field has
        been attempted, `initialized` is set to True. We don't require every
        registered field to be present — older cores may omit keys that were
        added later.
        """
        for key, value in init_data.items():
            self.apply_init_field(key, value)
        self.initialized = True

    def apply_init_field(self, key: str, value: Any) -> None:
        """Apply one `InitData` field. Subclasses usually use `@init_field`.

        The default implementation routes to a registered `@init_field(name)`
        handler, or logs-and-drops if no handler exists. Subclasses can
        override this method entirely if they need per-call logic that
        doesn't fit the one-handler-per-key shape.
        """
        handler = type(self)._init_fields.get(key)
        if handler is None:
            _log.debug(
                "%s(%r): unknown init field %r (value type %s)",
                type(self).__name__,
                self.object_name,
                key,
                type(value).__name__,
            )
            return
        try:
            handler(self, value)
        except (TypeError, ValueError) as exc:
            _log.warning(
                "%s(%r): init field %r raised %s: %s",
                type(self).__name__,
                self.object_name,
                key,
                type(exc).__name__,
                exc,
            )


__all__ = [
    "SyncObject",
    "init_field",
    "sync_slot",
]
