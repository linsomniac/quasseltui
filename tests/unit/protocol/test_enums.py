"""Tests for legacy binary feature flag helpers."""

from __future__ import annotations

from quasseltui.protocol.enums import (
    FEATURE_LONG_TIME,
    FEATURE_SENDER_PREFIXES,
    LEGACY_EXTENDED_FEATURES,
    LEGACY_SENDER_PREFIXES,
    bitmask_to_features,
    features_to_bitmask,
)


class TestFeaturesToBitmask:
    def test_sender_prefixes_maps_to_bit_13(self) -> None:
        mask = features_to_bitmask((FEATURE_SENDER_PREFIXES,))
        assert mask & LEGACY_SENDER_PREFIXES

    def test_features_without_legacy_bits_produce_zero(self) -> None:
        # LongTime has no legacy bit
        mask = features_to_bitmask((FEATURE_LONG_TIME,))
        assert mask == 0

    def test_empty_features_returns_zero(self) -> None:
        assert features_to_bitmask(()) == 0
        assert features_to_bitmask(frozenset()) == 0

    def test_full_default_features(self) -> None:
        from quasseltui.protocol.enums import DEFAULT_CLIENT_FEATURES

        mask = features_to_bitmask(DEFAULT_CLIENT_FEATURES)
        assert mask & LEGACY_SENDER_PREFIXES
        # ExtendedFeatures is NOT set (avoids compat issues with older cores)
        assert not (mask & LEGACY_EXTENDED_FEATURES)


class TestBitmaskToFeatures:
    def test_sender_prefixes_extracted(self) -> None:
        features = bitmask_to_features(0x2000)
        assert FEATURE_SENDER_PREFIXES in features

    def test_full_core_bitmask(self) -> None:
        # 0xfeff is what the target core advertises
        features = bitmask_to_features(0xFEFF)
        assert FEATURE_SENDER_PREFIXES in features

    def test_zero_bitmask_returns_empty(self) -> None:
        assert bitmask_to_features(0) == frozenset()

    def test_unrelated_bits_ignored(self) -> None:
        # Bits that have no string equivalent should not appear
        features = bitmask_to_features(0x0001)  # SynchronizedMarkerLine only
        assert len(features) == 0
