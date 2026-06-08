"""Deep-sleep capability helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES,
    DEFAULT_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES,
    MAX_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES,
    MIN_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES,
)


def supports_deep_sleep(device_config: object) -> bool:
    """Return whether the device configuration exposes deep sleep support."""
    power = getattr(device_config, "power", None)
    return hasattr(power, "deep_sleep_time_seconds")


def deep_sleep_seconds(device_config: object) -> int:
    """Return configured deep sleep seconds, clamped to non-negative values."""
    power = getattr(device_config, "power", None)
    raw_value = getattr(power, "deep_sleep_time_seconds", 0)
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return 0


def deep_sleep_enabled(device_config: object) -> bool:
    """Return whether deep sleep is currently enabled in device config."""
    return supports_deep_sleep(device_config) and deep_sleep_seconds(device_config) > 0


def deep_sleep_timeout_margin_minutes(options: Mapping[str, Any] | None) -> int:
    """Return the configured deep-sleep timeout margin in minutes."""
    raw_value = (
        options.get(CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES)
        if options is not None
        else None
    )
    if raw_value is None:
        return DEFAULT_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES
    try:
        margin = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES
    return min(
        MAX_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES,
        max(MIN_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES, margin),
    )


def availability_window_seconds(
    deep_sleep_time_seconds: int,
    timeout_margin_minutes: int = DEFAULT_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES,
) -> int:
    """Return how long a sleeping device should remain available."""
    try:
        sleep_seconds = max(0, int(deep_sleep_time_seconds))
    except (TypeError, ValueError):
        return 0
    if sleep_seconds <= 0:
        return 0
    margin_minutes = deep_sleep_timeout_margin_minutes(
        {CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES: timeout_margin_minutes}
    )
    return sleep_seconds + (margin_minutes * 60)
