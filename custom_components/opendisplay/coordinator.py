"""Passive BLE coordinator for OpenDisplay devices."""

from dataclasses import dataclass, field
from datetime import datetime
import logging
import math

from opendisplay import MANUFACTURER_ID, AdvertisementTracker, parse_advertisement
from opendisplay.models.advertisement import (
    AdvertisementData,
    ButtonChangeEvent,
    TouchChangeEvent,
    TouchTracker,
)

from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    MONOTONIC_TIME,
)
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES,
    DEFAULT_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES,
    SIGNAL_DEVICE_SEEN,
)
from .deep_sleep import (
    availability_window_seconds,
    deep_sleep_timeout_margin_minutes as normalize_timeout_margin_minutes,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)


def _utc_timestamp() -> float:
    """Return the current UTC timestamp using Home Assistant datetime helpers."""
    return dt_util.utcnow().timestamp()


def _service_info_time(service_info: BluetoothServiceInfoBleak) -> float | None:
    """Return monotonic BLE event time when Home Assistant provides it."""
    try:
        return float(service_info.time)
    except (AttributeError, TypeError, ValueError):
        return None


@dataclass
class OpenDisplayUpdate:
    """Parsed advertisement data for one OpenDisplay device."""

    address: str
    advertisement: AdvertisementData
    rssi: int | None = None
    last_seen: float | None = None
    last_seen_ble_time: float | None = None
    button_events: list[ButtonChangeEvent] = field(default_factory=list)
    touch_events: list[TouchChangeEvent] = field(default_factory=list)


class OpenDisplayCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Coordinator for passive BLE advertisement updates from an OpenDisplay device."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        deep_sleep_time_seconds: int = 0,
        deep_sleep_timeout_margin_minutes: int = (
            DEFAULT_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES
        ),
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            address,
            BluetoothScanningMode.PASSIVE,
            connectable=False,
        )
        self.data: OpenDisplayUpdate | None = None
        self._tracker: AdvertisementTracker = AdvertisementTracker()
        self.touch_trackers: list[TouchTracker] = []
        self.deep_sleep_time_seconds = max(0, int(deep_sleep_time_seconds))
        self.deep_sleep_timeout_margin_minutes = normalize_timeout_margin_minutes(
            {
                CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES: (
                    deep_sleep_timeout_margin_minutes
                )
            }
        )
        self._restored_last_seen: float | None = None
        self._startup_cache_started_at: float | None = None
        self._started_ble_time: float = MONOTONIC_TIME()
        self._last_service_info_time: float | None = None
        self._deep_sleep_deadline_unsub: CALLBACK_TYPE | None = None
        self._pending_upload = False
        # Subscribers notified once when the advertised reboot flag goes
        # False -> True (the device rebooted since we last talked to it).
        self._reboot_callbacks: set[CALLBACK_TYPE] = set()
        # Reboot-flag edge detection: the device sets the advertised reboot flag
        # on boot and clears it on first connect. None until the first v1 advert.
        self._last_reboot_flag: bool | None = None
        _LOGGER.debug(
            "%s: Coordinator initialized "
            "(deep_sleep=%ss, timeout_margin=%smin, availability_window=%ss, "
            "started_ble_time=%.3f)",
            self.address,
            self.deep_sleep_time_seconds,
            self.deep_sleep_timeout_margin_minutes,
            self.deep_sleep_availability_window_seconds,
            self._started_ble_time,
        )

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start Bluetooth callbacks and deep-sleep deadline tracking."""
        parent_unsub = super().async_start()
        self._async_schedule_deep_sleep_deadline()

        @callback
        def _async_stop() -> None:
            self._async_cancel_deep_sleep_deadline()
            parent_unsub()

        return _async_stop

    @property
    def deep_sleep_availability_window_seconds(self) -> int:
        """Return how long a sleeping device should remain available."""
        return availability_window_seconds(
            self.deep_sleep_time_seconds,
            self.deep_sleep_timeout_margin_minutes,
        )

    @callback
    def async_set_deep_sleep_time_seconds(self, value: int) -> None:
        """Set deep-sleep duration and reschedule availability deadline."""
        try:
            deep_sleep_time_seconds = max(0, int(value))
        except (TypeError, ValueError):
            deep_sleep_time_seconds = 0
        if deep_sleep_time_seconds != self.deep_sleep_time_seconds:
            _LOGGER.info(
                "%s: Deep sleep time changed from %ss to %ss",
                self.address,
                self.deep_sleep_time_seconds,
                deep_sleep_time_seconds,
            )
        self.deep_sleep_time_seconds = deep_sleep_time_seconds
        self._async_schedule_deep_sleep_deadline()
        self.async_update_listeners()

    @callback
    def async_startup_from_cache(self) -> None:
        """Assume cached startup begins inside the current deep-sleep interval."""
        self._available = False
        now = _utc_timestamp()
        self._startup_cache_started_at = now
        _LOGGER.debug(
            "%s: Startup from cached runtime data "
            "(startup_reference=%s, deep_sleep=%ss, availability_window=%ss)",
            self.address,
            dt_util.utc_from_timestamp(now).isoformat(),
            self.deep_sleep_time_seconds,
            self.deep_sleep_availability_window_seconds,
        )
        self._async_schedule_deep_sleep_deadline()

    def _align_restored_reference_to_current_cycle(
        self,
        reference_ts: float,
        now: float,
    ) -> float:
        """Align restored last_seen to the current deep-sleep cycle at startup."""
        if self.deep_sleep_time_seconds <= 0:
            return reference_ts
        availability_window = self.deep_sleep_availability_window_seconds
        if reference_ts + availability_window > now:
            return reference_ts
        margin_seconds = availability_window - self.deep_sleep_time_seconds
        cycle_number = max(
            1,
            math.ceil(
                (now - margin_seconds - reference_ts)
                / self.deep_sleep_time_seconds
            ),
        )
        return reference_ts + ((cycle_number - 1) * self.deep_sleep_time_seconds)

    @callback
    def async_restore_last_seen(self, value: datetime | float | int | None) -> None:
        """Restore last seen timestamp from Home Assistant stored sensor data."""
        if value is None:
            return
        if isinstance(value, datetime):
            timestamp = value.timestamp()
        else:
            try:
                timestamp = float(value)
            except (TypeError, ValueError):
                return
        if timestamp <= 0:
            return
        now = _utc_timestamp()
        original_timestamp = timestamp
        if (
            self._startup_cache_started_at is not None
            and self.deep_sleep_time_seconds > 0
        ):
            timestamp = self._align_restored_reference_to_current_cycle(
                timestamp,
                now,
            )
        self._restored_last_seen = timestamp
        if timestamp != original_timestamp:
            _LOGGER.info(
                "%s: Restored last_seen aligned to current deep-sleep cycle "
                "(restored_last_seen=%s, cycle_reference=%s, "
                "deep_sleep=%ss, timeout_margin=%smin, now=%s, "
                "expected_wakeup=%s, availability_deadline=%s)",
                self.address,
                dt_util.utc_from_timestamp(original_timestamp).isoformat(),
                dt_util.utc_from_timestamp(timestamp).isoformat(),
                self.deep_sleep_time_seconds,
                self.deep_sleep_timeout_margin_minutes,
                dt_util.utc_from_timestamp(now).isoformat(),
                self.expected_wakeup_timestamp.isoformat()
                if self.expected_wakeup_timestamp
                else None,
                self.deep_sleep_availability_deadline_timestamp.isoformat()
                if self.deep_sleep_availability_deadline_timestamp
                else None,
            )
        _LOGGER.debug(
            "%s: Restored last_seen for deep-sleep availability "
            "(last_seen=%s, expected_wakeup=%s, availability_deadline=%s)",
            self.address,
            dt_util.utc_from_timestamp(timestamp).isoformat(),
            self.expected_wakeup_timestamp.isoformat()
            if self.expected_wakeup_timestamp
            else None,
            self.deep_sleep_availability_deadline_timestamp.isoformat()
            if self.deep_sleep_availability_deadline_timestamp
            else None,
        )
        self._async_schedule_deep_sleep_deadline()
        self.async_update_listeners()

    @property
    def available(self) -> bool:
        """Return availability with deep-sleep grace semantics."""
        if self.deep_sleep_time_seconds <= 0:
            return super().available

        now = _utc_timestamp()
        if (reference_ts := self._sleep_reference_timestamp) is not None:
            return (
                now - reference_ts
            ) < self.deep_sleep_availability_window_seconds

        return super().available

    @property
    def pending_upload(self) -> bool:
        """Return whether this coordinator currently has a pending upload."""
        return self._pending_upload

    @callback
    def async_set_pending_upload(self, value: bool) -> None:
        """Set pending upload state used by diagnostic entities."""
        if self._pending_upload != value:
            _LOGGER.debug(
                "%s: Pending upload flag changed to %s",
                self.address,
                value,
            )
        self._pending_upload = value
        self.async_update_listeners()

    @property
    def expected_wakeup_timestamp(self) -> datetime | None:
        """Return expected wake-up timestamp based on last seen and deep sleep."""
        if self.deep_sleep_time_seconds <= 0:
            return None
        reference_ts = self._sleep_reference_timestamp
        if reference_ts is None:
            return None
        return dt_util.utc_from_timestamp(
            reference_ts + self.deep_sleep_time_seconds
        )

    @property
    def deep_sleep_availability_deadline_timestamp(self) -> datetime | None:
        """Return when the current deep-sleep availability window expires."""
        if self.deep_sleep_time_seconds <= 0:
            return None
        reference_ts = self._sleep_reference_timestamp
        if reference_ts is None:
            return None
        return dt_util.utc_from_timestamp(
            reference_ts + self.deep_sleep_availability_window_seconds
        )

    @property
    def _sleep_reference_timestamp(self) -> float | None:
        """Return the timestamp used as the beginning of the sleep interval."""
        if self.data is not None and self.data.last_seen is not None:
            return self.data.last_seen
        if self._restored_last_seen is not None:
            return self._restored_last_seen
        return self._startup_cache_started_at

    def _is_expected_sleep(self) -> bool:
        """Return True when unavailable is expected inside deep-sleep window."""
        if self.deep_sleep_time_seconds <= 0:
            return False
        reference_ts = self._sleep_reference_timestamp
        if reference_ts is None:
            return False
        deadline = reference_ts + self.deep_sleep_availability_window_seconds
        now = _utc_timestamp()
        _LOGGER.debug(
            "%s: Expected sleep window check "
            "(sleep_reference=%.3f, sleep=%ss, availability_window=%ss, "
            "deadline=%.3f, now=%.3f)",
            self.address,
            reference_ts,
            self.deep_sleep_time_seconds,
            self.deep_sleep_availability_window_seconds,
            deadline,
            now,
        )
        return now < deadline

    @callback
    def _async_cancel_deep_sleep_deadline(self) -> None:
        """Cancel any scheduled deep-sleep availability deadline callback."""
        if self._deep_sleep_deadline_unsub is None:
            return
        self._deep_sleep_deadline_unsub()
        self._deep_sleep_deadline_unsub = None

    @callback
    def _async_schedule_deep_sleep_deadline(self) -> None:
        """Schedule a state update when the deep-sleep availability window expires."""
        self._async_cancel_deep_sleep_deadline()
        if self.deep_sleep_time_seconds <= 0:
            return
        deadline = self.deep_sleep_availability_deadline_timestamp
        if deadline is None:
            return
        now = _utc_timestamp()
        if deadline.timestamp() <= now:
            _LOGGER.debug(
                "%s: Deep-sleep availability deadline already expired "
                "(deadline=%s, now=%s)",
                self.address,
                deadline.isoformat(),
                dt_util.utc_from_timestamp(now).isoformat(),
            )
            return
        self._deep_sleep_deadline_unsub = async_track_point_in_utc_time(
            self.hass,
            self._async_deep_sleep_deadline_reached,
            deadline,
        )
        _LOGGER.debug(
            "%s: Deep-sleep availability deadline scheduled "
            "(deadline=%s, expected_wakeup=%s, availability_window=%ss)",
            self.address,
            deadline.isoformat(),
            self.expected_wakeup_timestamp.isoformat()
            if self.expected_wakeup_timestamp
            else None,
            self.deep_sleep_availability_window_seconds,
        )

    @callback
    def _async_deep_sleep_deadline_reached(self, _now: datetime) -> None:
        """Refresh listeners when the deep-sleep availability deadline is reached."""
        self._deep_sleep_deadline_unsub = None
        if self._is_expected_sleep():
            self._async_schedule_deep_sleep_deadline()
            return
        _LOGGER.info(
            "%s: Deep-sleep availability window expired; marking device unavailable",
            self.address,
        )
        self._available = False
        self.async_update_listeners()

    def _is_stale_bluetooth_event(
        self,
        service_info: BluetoothServiceInfoBleak,
        service_time: float | None,
    ) -> bool:
        """Return True when a BLE callback is older than the current session."""
        if service_time is None:
            return False

        if service_time < self._started_ble_time:
            _LOGGER.debug(
                "%s: Ignoring restored Bluetooth advertisement "
                "(ble_time=%.3f, coordinator_started_ble_time=%.3f, "
                "has_opendisplay_manufacturer_data=%s, available=%s)",
                service_info.address,
                service_time,
                self._started_ble_time,
                MANUFACTURER_ID in service_info.manufacturer_data,
                self.available,
            )
            return True

        if (
            self._last_service_info_time is not None
            and service_time <= self._last_service_info_time
        ):
            _LOGGER.debug(
                "%s: Ignoring duplicate or older Bluetooth advertisement "
                "(ble_time=%.3f, last_ble_time=%.3f, "
                "has_opendisplay_manufacturer_data=%s, available=%s)",
                service_info.address,
                service_time,
                self._last_service_info_time,
                MANUFACTURER_ID in service_info.manufacturer_data,
                self.available,
            )
            return True

        return False

    @callback
    def async_subscribe_reboot(self, callback_: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Subscribe to device reboots (advertised reboot flag False -> True).

        Returns an unsubscribe callback.
        """
        self._reboot_callbacks.add(callback_)

        @callback
        def _unsubscribe() -> None:
            self._reboot_callbacks.discard(callback_)

        return _unsubscribe

    @callback
    def _async_handle_unavailable(
        self, service_info: BluetoothServiceInfoBleak
    ) -> None:
        """Handle the device going unavailable."""
        if self._is_expected_sleep():
            _LOGGER.debug(
                "%s: Device is in expected deep sleep window; availability unchanged",
                service_info.address,
            )
            return
        if self._available:
            _LOGGER.info("%s: Device is unavailable", service_info.address)
        super()._async_handle_unavailable(service_info)

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Handle a Bluetooth advertisement event."""
        parsed_update: OpenDisplayUpdate | None = None
        service_time = _service_info_time(service_info)
        _LOGGER.debug(
            "%s: Bluetooth event received "
            "(change=%s, ble_time=%s, coordinator_started_ble_time=%.3f, "
            "last_ble_time=%s, rssi=%s, connectable=%s, "
            "has_opendisplay_manufacturer_data=%s, available_before=%s, "
            "deep_sleep=%ss, expected_wakeup=%s, availability_deadline=%s)",
            service_info.address,
            change,
            service_time,
            self._started_ble_time,
            self._last_service_info_time,
            getattr(service_info, "rssi", None),
            getattr(service_info, "connectable", None),
            MANUFACTURER_ID in service_info.manufacturer_data,
            self.available,
            self.deep_sleep_time_seconds,
            self.expected_wakeup_timestamp.isoformat()
            if self.expected_wakeup_timestamp
            else None,
            self.deep_sleep_availability_deadline_timestamp.isoformat()
            if self.deep_sleep_availability_deadline_timestamp
            else None,
        )
        if self._is_stale_bluetooth_event(service_info, service_time):
            return

        if MANUFACTURER_ID not in service_info.manufacturer_data:
            _LOGGER.debug(
                "%s: Ignoring Bluetooth advertisement without OpenDisplay "
                "manufacturer data",
                service_info.address,
            )
            return

        try:
            advertisement = parse_advertisement(
                service_info.manufacturer_data[MANUFACTURER_ID]
            )
        except ValueError as err:
            _LOGGER.debug(
                "%s: Failed to parse advertisement data: %s",
                service_info.address,
                err,
                exc_info=True,
            )
            return
        else:
            self._check_reboot_flag(advertisement)
            button_events = self._tracker.update(service_info.address, advertisement)
            touch_events: list[TouchChangeEvent] = []
            for touch_tracker in self.touch_trackers:
                touch_events.extend(
                    touch_tracker.update(service_info.address, advertisement)
                )
            parsed_update = OpenDisplayUpdate(
                address=service_info.address,
                advertisement=advertisement,
                rssi=service_info.rssi,
                last_seen=_utc_timestamp(),
                last_seen_ble_time=service_time,
                button_events=button_events,
                touch_events=touch_events,
            )
            self.data = parsed_update
            self._restored_last_seen = None
            self._startup_cache_started_at = None
            self._last_service_info_time = service_time
            if not self._available:
                _LOGGER.info("%s: Device is available again", service_info.address)
            _LOGGER.debug(
                "%s: Advertisement parsed (rssi=%s, button_events=%s, "
                "touch_events=%s, ble_time=%s, last_seen=%s, expected_wakeup=%s, "
                "availability_deadline=%s); signaling device seen",
                service_info.address,
                service_info.rssi,
                len(button_events),
                len(touch_events),
                service_time,
                dt_util.utc_from_timestamp(parsed_update.last_seen).isoformat()
                if parsed_update.last_seen is not None
                else None,
                self.expected_wakeup_timestamp.isoformat()
                if self.expected_wakeup_timestamp
                else None,
                self.deep_sleep_availability_deadline_timestamp.isoformat()
                if self.deep_sleep_availability_deadline_timestamp
                else None,
            )
            async_dispatcher_send(
                self.hass,
                f"{SIGNAL_DEVICE_SEEN}_{service_info.address}",
            )

        super()._async_handle_bluetooth_event(service_info, change)

        # Parent coordinator can store raw Bluetooth service info in self.data.
        # Restore parsed OpenDisplay payload so sensors always see typed fields.
        if parsed_update is not None:
            self.data = parsed_update
            self._async_schedule_deep_sleep_deadline()
            self.async_update_listeners()

    @callback
    def _check_reboot_flag(self, advertisement: AdvertisementData) -> None:
        """Notify on a reboot, detected as a reboot-flag False -> True edge.

        The device sets the advertised reboot flag on boot and clears it on the
        first BLE connection. We react only to a False -> True transition: the
        initial observation (None -> True) is ignored because setup already
        synced this boot, and a flag that stays True (True -> True, e.g. a device
        that never clears it) is self-guarding and won't fire again.
        """
        reboot_flag = advertisement.reboot_flag
        if reboot_flag is None:
            # Legacy advertisement: no reboot flag, leave previous state intact.
            return

        previous = self._last_reboot_flag
        self._last_reboot_flag = reboot_flag

        if reboot_flag and previous is False and self._reboot_callbacks:
            _LOGGER.info("%s: Device rebooted since last connection", self.address)
            for reboot_callback in list(self._reboot_callbacks):
                reboot_callback()
