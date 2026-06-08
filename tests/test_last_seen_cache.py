"""Tests for cached last_seen persistence."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.opendisplay import (
    _cache_last_seen,
    _cached_last_seen,
    _normalize_entry_data,
)
from custom_components.opendisplay.const import CONF_CACHED_LAST_SEEN


def test_cached_last_seen_accepts_numeric_values() -> None:
    """Cached last_seen should be restored as a positive timestamp."""
    assert _cached_last_seen({CONF_CACHED_LAST_SEEN: "123.5"}) == 123.5


def test_normalize_entry_data_drops_invalid_cached_last_seen() -> None:
    """Invalid cached last_seen values should not stay in entry data."""
    normalized = _normalize_entry_data({CONF_CACHED_LAST_SEEN: "not-a-timestamp"})

    assert CONF_CACHED_LAST_SEEN not in normalized


def test_cache_last_seen_throttles_small_updates() -> None:
    """last_seen writes should be throttled for chatty BLE advertisements."""
    hass = SimpleNamespace(config_entries=MagicMock())
    entry = SimpleNamespace(data={CONF_CACHED_LAST_SEEN: 1000.0})

    _cache_last_seen(hass, entry, 1030.0)

    hass.config_entries.async_update_entry.assert_not_called()


def test_cache_last_seen_persists_after_throttle_window() -> None:
    """last_seen should be persisted when enough time has passed."""
    hass = SimpleNamespace(config_entries=MagicMock())
    entry = SimpleNamespace(data={CONF_CACHED_LAST_SEEN: 1000.0})

    _cache_last_seen(hass, entry, 1061.0)

    hass.config_entries.async_update_entry.assert_called_once_with(
        entry,
        data={CONF_CACHED_LAST_SEEN: 1061.0},
    )
