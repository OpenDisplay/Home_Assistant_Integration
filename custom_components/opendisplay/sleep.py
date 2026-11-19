"""Deep-sleep awareness for OpenDisplay devices.

`SleepProfile` resolves the integration options and the device's own power
configuration into the three facts the rest of the integration needs:

* ``is_sleepy`` — should this device be treated as a deep-sleeping tag (dark
  most of the time, reachable only during a short post-wake advertising
  window)?
* ``availability_interval`` — how long HA should keep entities available
  between wakes before flagging the device as gone.
* ``probably_asleep(last_seen)`` — given the last advertisement time, is the
  device almost certainly back asleep right now?

The predicate for "sleepy" mirrors the firmware's own deep-sleep entry
condition (``power_mode == BATTERY and deep_sleep_time_seconds > 0``, see
``Firmware/src/main.cpp``) so the integration and the device can never disagree,
with an options override for edge cases. All computation here is pure and
side-effect free; the values are consumed by ``__init__``, ``services`` and the
delivery manager.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import TYPE_CHECKING

from opendisplay.models.enums import PowerMode

from .const import (
    CONF_MISSED_CYCLES,
    CONF_PROBE_BEFORE_QUEUE,
    CONF_QUEUE_TIMEOUT_HOURS,
    CONF_SLEEP_AFTER_DELIVERY,
    CONF_SLEEP_MODE,
    DEFAULT_MISSED_CYCLES,
    DEFAULT_PROBE_BEFORE_QUEUE,
    DEFAULT_QUEUE_TIMEOUT_HOURS,
    DEFAULT_SLEEP_AFTER_DELIVERY,
    DEFAULT_SLEEP_MODE,
    SLEEP_MODE_OFF,
    SLEEP_MODE_ON,
)

if TYPE_CHECKING:
    from opendisplay import GlobalConfig

    from . import OpenDisplayConfigEntry

# Firmware default post-wake advertising window when sleep_timeout_ms is 0
# (Firmware/src/main.cpp:94-96). Expressed in milliseconds to match the config.
DEFAULT_WAKE_WINDOW_MS = 10_000
# Extra slack added on top of the wake window when deciding the device is back
# asleep, to absorb scanner/proxy advertisement latency.
FRESHNESS_SLACK_S = 5.0
# Fixed slack added to the availability horizon so a marginally late wake does
# not immediately flap entities unavailable.
AVAILABILITY_SLACK_S = 60.0


@dataclass(frozen=True)
class SleepProfile:
    """Resolved deep-sleep behavior for one config entry."""

    is_sleepy: bool
    deep_sleep_enabled: bool
    deep_sleep_time_seconds: int
    sleep_timeout_ms: int
    missed_cycles: int
    queue_timeout_hours: int
    probe_before_queue: bool
    sleep_after_delivery: bool = DEFAULT_SLEEP_AFTER_DELIVERY

    @property
    def wake_window_s(self) -> float:
        """Post-wake advertising window in seconds (firmware default 10 s)."""
        return (self.sleep_timeout_ms or DEFAULT_WAKE_WINDOW_MS) / 1000.0

    @property
    def availability_interval(self) -> float:
        """Seconds of silence tolerated before entities go unavailable.

        ``deep_sleep_time_seconds * missed_cycles`` (the device may skip a few
        expected wakes) plus one wake window and a fixed slack.
        """
        return (
            self.deep_sleep_time_seconds * self.missed_cycles
            + self.wake_window_s
            + AVAILABILITY_SLACK_S
        )

    @property
    def queue_timeout_s(self) -> float:
        """Seconds a queued upload survives before expiring."""
        return self.queue_timeout_hours * 3600

    def probably_asleep(
        self, last_seen: float | None, now: float | None = None
    ) -> bool:
        """Return True if the device is almost certainly back asleep.

        The device advertises only during its wake window. If the most recent
        advertisement is older than one wake window plus slack, it has returned
        to deep sleep and connecting would just burn a doomed retry cycle. A
        device never seen (``last_seen is None``) is treated as asleep.

        This is a pure freshness test independent of ``is_sleepy``; callers
        combine it with ``is_sleepy`` where the distinction matters.
        """
        if last_seen is None:
            return True
        current = time.time() if now is None else now
        return (current - last_seen) > (self.wake_window_s + FRESHNESS_SLACK_S)

    @classmethod
    def create(
        cls,
        *,
        sleep_mode: str,
        power_mode: int,
        sleep_timeout_ms: int,
        deep_sleep_time_seconds: int,
        missed_cycles: int,
        queue_timeout_hours: int,
        probe_before_queue: bool = DEFAULT_PROBE_BEFORE_QUEUE,
        sleep_after_delivery: bool = DEFAULT_SLEEP_AFTER_DELIVERY,
    ) -> SleepProfile:
        """Build a profile from primitive values (pure; unit-testable)."""
        deep_sleep_enabled = (
            power_mode == PowerMode.BATTERY and deep_sleep_time_seconds > 0
        )
        if sleep_mode == SLEEP_MODE_ON:
            is_sleepy = True
        elif sleep_mode == SLEEP_MODE_OFF:
            is_sleepy = False
        else:  # SLEEP_MODE_AUTO
            is_sleepy = deep_sleep_enabled
        return cls(
            is_sleepy=is_sleepy,
            deep_sleep_enabled=deep_sleep_enabled,
            deep_sleep_time_seconds=deep_sleep_time_seconds,
            sleep_timeout_ms=sleep_timeout_ms,
            missed_cycles=missed_cycles,
            queue_timeout_hours=queue_timeout_hours,
            probe_before_queue=probe_before_queue,
            sleep_after_delivery=sleep_after_delivery,
        )

    @classmethod
    def from_entry(
        cls, entry: OpenDisplayConfigEntry, config: GlobalConfig
    ) -> SleepProfile:
        """Build a profile from a config entry's options and device config."""
        options = entry.options
        power = config.power
        return cls.create(
            sleep_mode=options.get(CONF_SLEEP_MODE, DEFAULT_SLEEP_MODE),
            power_mode=power.power_mode,
            sleep_timeout_ms=power.sleep_timeout_ms,
            deep_sleep_time_seconds=power.deep_sleep_time_seconds,
            missed_cycles=options.get(CONF_MISSED_CYCLES, DEFAULT_MISSED_CYCLES),
            queue_timeout_hours=options.get(
                CONF_QUEUE_TIMEOUT_HOURS, DEFAULT_QUEUE_TIMEOUT_HOURS
            ),
            probe_before_queue=options.get(
                CONF_PROBE_BEFORE_QUEUE, DEFAULT_PROBE_BEFORE_QUEUE
            ),
            sleep_after_delivery=options.get(
                CONF_SLEEP_AFTER_DELIVERY, DEFAULT_SLEEP_AFTER_DELIVERY
            ),
        )
