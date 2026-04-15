"""QMetaType numeric IDs used in Qt's QVariant wire format.

Source-of-truth: Qt 5/6 `qmetatype.h` (the `QMetaType::Type` enum).
Quassel uses the standard Qt values, so we mirror them here. New IDs are added
to this file as later phases need them.
"""

from __future__ import annotations

from enum import IntEnum


class QMetaType(IntEnum):
    """Qt meta-type IDs as used in QVariant serialization."""

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
    # Phase 4 will add QDate (14), QTime (15), QDateTime (16) for IRC
    # message timestamps. Keep this list in sync with the dispatch table in
    # `quasseltui.qt.variant`.
    UserType = 127
