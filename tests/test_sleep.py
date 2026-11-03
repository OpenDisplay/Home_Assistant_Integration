"""Tests for the pure SleepProfile logic."""

from collections.abc import Awaitable, Callable

from homeassistant.const import Platform
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.opendisplay.const import (
    CONF_MISSED_CYCLES,
    CONF_PROBE_BEFORE_QUEUE,
    CONF_QUEUE_TIMEOUT_HOURS,
    CONF_SLEEP_MODE,
    DEFAULT_PROBE_BEFORE_QUEUE,
    DEFAULT_QUEUE_TIMEOUT_HOURS,
)
from custom_components.opendisplay.sleep import (
    AVAILABILITY_SLACK_S,
    DEFAULT_WAKE_WINDOW_MS,
    FRESHNESS_SLACK_S,
    SleepProfile,
)

from . import make_sleepy_device_config


def _profile(**overrides):
    """Build a SleepProfile from sensible defaults overridden per test."""
    params = {
        "sleep_mode": "auto",
        "power_mode": 1,  # BATTERY
        "sleep_timeout_ms": 0,
        "deep_sleep_time_seconds": 300,
        "missed_cycles": 3,
        "queue_timeout_hours": 24,
    }
    params.update(overrides)
    return SleepProfile.create(**params)


def test_deep_sleep_enabled_requires_battery_and_interval():
    """Deep sleep needs both battery power and a configured interval."""
    assert _profile(power_mode=1, deep_sleep_time_seconds=300).deep_sleep_enabled
    # USB power → not a deep-sleeper even with an interval set.
    assert not _profile(power_mode=2, deep_sleep_time_seconds=300).deep_sleep_enabled
    # Battery but no interval → not a deep-sleeper.
    assert not _profile(power_mode=1, deep_sleep_time_seconds=0).deep_sleep_enabled


def test_sleep_mode_auto_follows_device():
    """In auto mode the device's own configuration decides."""
    assert _profile(
        sleep_mode="auto", power_mode=1, deep_sleep_time_seconds=300
    ).is_sleepy
    assert not _profile(
        sleep_mode="auto", power_mode=2, deep_sleep_time_seconds=300
    ).is_sleepy


def test_sleep_mode_force_on_and_off_override_device():
    """An explicit sleep mode overrides what the device reports."""
    # Force on even for a USB device with no deep sleep.
    assert _profile(sleep_mode="on", power_mode=2, deep_sleep_time_seconds=0).is_sleepy
    # Force off even for a battery device configured for deep sleep.
    forced_off = _profile(sleep_mode="off", power_mode=1, deep_sleep_time_seconds=300)
    assert not forced_off.is_sleepy
    # deep_sleep_enabled still reflects the device, independent of the override.
    assert forced_off.deep_sleep_enabled


def test_wake_window_uses_firmware_default_when_zero():
    """A zero timeout means the firmware default, not a zero-length window."""
    assert _profile(sleep_timeout_ms=0).wake_window_s == DEFAULT_WAKE_WINDOW_MS / 1000.0
    assert _profile(sleep_timeout_ms=5000).wake_window_s == 5.0


def test_availability_interval_formula():
    """Availability spans several sleep cycles plus the wake window and slack."""
    profile = _profile(sleep_timeout_ms=0, deep_sleep_time_seconds=300, missed_cycles=3)
    # 300 * 3 + 10 (default window) + 60 slack = 970
    expected = 300 * 3 + DEFAULT_WAKE_WINDOW_MS / 1000.0 + AVAILABILITY_SLACK_S
    assert profile.availability_interval == expected == 970.0


def test_queue_timeout_seconds():
    """The configured queue timeout is exposed in seconds."""
    assert _profile(queue_timeout_hours=24).queue_timeout_s == 86400
    assert _profile(queue_timeout_hours=1).queue_timeout_s == 3600


def test_probably_asleep_never_seen_is_true():
    """A device never seen is assumed asleep."""
    assert _profile().probably_asleep(None) is True


def test_probably_asleep_recent_advert_is_false():
    """An advertisement inside the wake window means the device is awake."""
    profile = _profile(sleep_timeout_ms=10000)  # 10 s window
    now = 1_000_000.0
    # Seen 1 s ago: still inside the wake window -> may be awake.
    assert profile.probably_asleep(now - 1.0, now=now) is False


def test_probably_asleep_stale_advert_is_true():
    """An advertisement older than the window means it went back to sleep."""
    profile = _profile(sleep_timeout_ms=10000)
    now = 1_000_000.0
    # Seen well beyond window + slack -> back asleep.
    stale = now - (10.0 + FRESHNESS_SLACK_S + 1.0)
    assert profile.probably_asleep(stale, now=now) is True


def test_probe_before_queue_defaults_true():
    """Probing before queueing is on unless turned off."""
    # Default also proves create() keeps working for callers that omit it.
    assert _profile().probe_before_queue is True


def test_probe_before_queue_override():
    """The probe can be disabled explicitly."""
    assert _profile(probe_before_queue=False).probe_before_queue is False


def test_probably_asleep_boundary():
    """The freshness check is inclusive at the boundary."""
    profile = _profile(sleep_timeout_ms=10000)
    now = 1_000_000.0
    threshold = 10.0 + FRESHNESS_SLACK_S
    # Exactly at the threshold is not yet "asleep" (strict greater-than).
    assert profile.probably_asleep(now - threshold, now=now) is False
    assert profile.probably_asleep(now - threshold - 0.01, now=now) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# --- from_entry ------------------------------------------------------------
#
# The tests above cover the pure predicates via create(**primitives). These
# cover the adapter that production actually calls: every value it wires out of
# entry.options and device_config.power, which create() never sees.


@pytest.fixture
def platforms() -> list[Platform]:
    """No platforms; these tests only need runtime_data."""
    return []


@pytest.mark.parametrize("device_config", [make_sleepy_device_config()])
async def test_from_entry_reads_the_device_power_config(
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """The sleep timings come from the device's own power packet."""
    await setup_entry()

    profile = mock_config_entry.runtime_data.sleep_profile
    power = make_sleepy_device_config().power
    assert profile.is_sleepy is True
    assert profile.wake_window_s == power.sleep_timeout_ms / 1000.0
    assert profile.deep_sleep_enabled is True


@pytest.mark.parametrize(
    "config_entry_options",
    [
        {
            CONF_SLEEP_MODE: "on",
            CONF_MISSED_CYCLES: 7,
            CONF_QUEUE_TIMEOUT_HOURS: 3,
            CONF_PROBE_BEFORE_QUEUE: False,
        }
    ],
)
async def test_from_entry_applies_every_configured_option(
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Each option reaches the profile it configures.

    This is the mapping create() cannot check: a mistyped CONF_ constant or a
    swapped default would leave the option silently ignored.
    """
    await setup_entry()

    profile = mock_config_entry.runtime_data.sleep_profile
    assert profile.is_sleepy is True  # forced on despite a USB device
    assert profile.queue_timeout_s == 3 * 3600
    assert profile.probe_before_queue is False
    assert profile.missed_cycles == 7


async def test_from_entry_falls_back_to_the_defaults(
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """An entry with no options configured still builds a usable profile."""
    await setup_entry()

    profile = mock_config_entry.runtime_data.sleep_profile
    assert profile.queue_timeout_s == DEFAULT_QUEUE_TIMEOUT_HOURS * 3600
    assert profile.probe_before_queue is DEFAULT_PROBE_BEFORE_QUEUE
