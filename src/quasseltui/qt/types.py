"""Numeric type IDs used in Quassel's QVariant wire format.

Source-of-truth: `src/common/serializers/serializers.h::Types::VariantType` in
the Quassel C++ source tree. **These are NOT raw Qt QMetaType IDs.** Quassel
defines its own value table — most entries match `QMetaType::Type` from Qt
4/5 (`Bool=1`, `Int=2`, `QString=10`, ...), but a handful diverge:

- `UserType = 127` (Qt 5/6 use `1024`).
- `Short = 130`, `UShort = 133`, `Long = 129`, `ULong = 132`,
  `Char = 131`, `UChar = 134` (Qt 5 uses `33/36/32/35/34/37`).

Quassel keeps its own values stable across Qt 4/5/6 because the wire format
is pinned to `QDataStream::Qt_4_2`. We MUST mirror Quassel's table, not Qt's,
or our QVariant envelopes will be unparseable by a real core.

The class is still named `QMetaType` for muscle memory — it's the meta-type
ID dispatch in our codec — but every value here is checked against Quassel's
header, not Qt's. New IDs are added on demand as later phases need them.
"""

from __future__ import annotations

from enum import IntEnum


class QMetaType(IntEnum):
    """Quassel's `Types::VariantType` numeric IDs as used on the wire."""

    Invalid = 0
    Bool = 1
    Int = 2
    UInt = 3
    LongLong = 4
    ULongLong = 5
    Double = 6
    QChar = 7
    QVariantMap = 8
    QVariantList = 9
    QString = 10
    QStringList = 11
    QByteArray = 12
    QDateTime = 16
    # Quassel-specific integer subtypes (NOT Qt's values).
    Short = 130
    UserType = 127
