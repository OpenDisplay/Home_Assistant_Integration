"""Tests for OpenDisplayCoordinator connectable=False fix.

Verifies that the coordinator uses connectable=False so that non-connectable
advertisements (e.g. from a device waking from deep sleep) are processed and
the entity is correctly reported as available.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import time


from custom_components.opendisplay.coordinator import (
    BluetoothChange,
    BluetoothScanningMode,
    OpenDisplayCoordinator,
    OpenDisplayUpdate,
)


def _make_coordinator(address: str = "AA:BB:CC:DD:EE:FF") -> OpenDisplayCoordinator:
    """Create a minimal OpenDisplayCoordinator with a mock hass."""
    hass = MagicMock()
    with patch(
        "custom_components.opendisplay.coordinator.PassiveBluetoothDataUpdateCoordinator.__init__",
        return_value=None,
    ) as mock_super:
        coord = OpenDisplayCoordinator(hass, address)
        mock_super.assert_called_once_with(
            hass,
            coord._LOGGER if hasattr(coord, "_LOGGER") else MagicMock(),
            address,
            BluetoothScanningMode.PASSIVE,
            connectable=False,
        )
    return coord


def test_coordinator_registers_with_connectable_false() -> None:
    """Coordinator must pass connectable=False to PassiveBluetoothDataUpdateCoordinator.

    This ensures the coordinator receives BLE advertisements from non-connectable
    devices, which is the state a device is in immediately after waking from deep
    sleep before it is ready to accept a connection.
    """
    hass = MagicMock()

    init_kwargs: dict = {}

    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        init_kwargs.update(kwargs)
        init_kwargs["args"] = args
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")

    with patch(original_init, _capture_init):
        OpenDisplayCoordinator(hass, "AA:BB:CC:DD:EE:FF")

    assert init_kwargs.get("connectable") is False, (
        "connectable must be False so non-connectable deep-sleep wake advertisements "
        "are processed by the coordinator"
    )


def test_coordinator_connectable_true_would_miss_deep_sleep_wakeup() -> None:
    """Document that connectable=True would cause missed deep-sleep wakeup events.

    When a device wakes from deep sleep it first broadcasts a non-connectable
    advertisement. A coordinator registered with connectable=True would not
    receive that event, making the entity appear unavailable during the wakeup
    window when the upload should be flushed.
    """
    hass = MagicMock()
    captured: dict = {}

    def _capture_init(self, *args, **kwargs):
        captured["connectable"] = kwargs.get("connectable")
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")

    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )
    with patch(original_init, _capture_init):
        OpenDisplayCoordinator(hass, "AA:BB:CC:DD:EE:FF")

    # The fix: must NOT be True (which would exclude non-connectable advertisements)
    assert captured.get("connectable") is not True, (
        "connectable=True would exclude non-connectable advertisements from deep-sleep "
        "devices; the coordinator must use connectable=False"
    )


def test_startup_from_cache_ignores_non_opendisplay_advertisement() -> None:
    """Cached startup should only trust fresh OpenDisplay advertisements."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(hass, "AA:BB:CC:DD:EE:FF")

    coordinator._started_ble_time = 1000.0
    coordinator.async_startup_from_cache()
    assert coordinator.available is False

    service_info = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        time=1001.0,
        manufacturer_data={},
    )

    with patch(
        "homeassistant.components.bluetooth.passive_update_coordinator."
        "PassiveBluetoothDataUpdateCoordinator._async_handle_bluetooth_event"
    ) as mock_super:
        coordinator._async_handle_bluetooth_event(
            service_info,
            BluetoothChange.ADVERTISEMENT,
        )

    mock_super.assert_not_called()
    assert coordinator.available is False
    assert coordinator._last_service_info_time is None


def test_startup_from_cache_ignores_restored_bluetooth_history() -> None:
    """Bluetooth history from before coordinator start must not mark device online."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(hass, "AA:BB:CC:DD:EE:FF")

    coordinator._started_ble_time = 1000.0
    coordinator.async_startup_from_cache()

    service_info = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        time=999.0,
        manufacturer_data={},
    )

    with patch(
        "homeassistant.components.bluetooth.passive_update_coordinator."
        "PassiveBluetoothDataUpdateCoordinator._async_handle_bluetooth_event"
    ) as mock_super:
        coordinator._async_handle_bluetooth_event(
            service_info,
            BluetoothChange.ADVERTISEMENT,
        )

    mock_super.assert_not_called()
    assert coordinator.available is False


def test_expected_sleep_window_uses_default_timeout_margin() -> None:
    """Unavailable is suppressed for the deep-sleep availability window."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = True

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=120,
        )

    now = time.time()
    coordinator.data = SimpleNamespace(last_seen=now - 539)
    assert coordinator._is_expected_sleep() is True

    coordinator.data = SimpleNamespace(last_seen=now - 541)
    assert coordinator._is_expected_sleep() is False


def test_deep_sleep_availability_window_adds_timeout_margin() -> None:
    """Long sleep intervals should add the configured timeout margin."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False
        self._listeners = {}

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=24 * 60 * 60,
        )

    assert coordinator.deep_sleep_availability_window_seconds == (
        24 * 60 * 60 + 7 * 60
    )


def test_deep_sleep_availability_window_uses_configured_timeout_margin() -> None:
    """Options can tune the deep-sleep timeout margin."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False
        self._listeners = {}

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=120,
            deep_sleep_timeout_margin_minutes=10,
        )

    assert coordinator.deep_sleep_timeout_margin_minutes == 10
    assert coordinator.deep_sleep_availability_window_seconds == 720


def test_expected_wakeup_timestamp_reflects_last_seen_plus_deep_sleep() -> None:
    """Expected wakeup timestamp should follow last_seen + deep_sleep."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = True

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=300,
        )

    coordinator.data = SimpleNamespace(last_seen=1000.0)
    expected = coordinator.expected_wakeup_timestamp
    assert expected is not None
    assert expected.timestamp() == 1300.0


def test_available_remains_true_within_deep_sleep_grace_after_last_seen() -> None:
    """Deep-sleep devices should stay available for grace window."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False
        self._listeners = {}

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=300,
        )

    now = time.time()
    coordinator.data = SimpleNamespace(last_seen=now - 60)
    assert coordinator.available is True


def test_available_false_after_deep_sleep_window_even_if_base_available() -> None:
    """Stale deep-sleep devices should become unavailable after wake window."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = True

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=120,
        )

    now = time.time()
    coordinator.data = SimpleNamespace(last_seen=now - 360)
    assert coordinator.available is True

    coordinator.data = SimpleNamespace(last_seen=now - 541)
    assert coordinator.available is False


def test_available_true_during_startup_cache_fallback_window() -> None:
    """Startup cache fallback should keep deep-sleep device available."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=300,
        )

    coordinator.async_startup_from_cache()
    assert coordinator.available is True


def test_available_false_after_startup_cache_sleep_window_expires() -> None:
    """Cached startup should expire when the device misses its wake window."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=300,
        )

    with patch(
        "custom_components.opendisplay.coordinator._utc_timestamp",
        return_value=1000,
    ):
        coordinator.async_startup_from_cache()
        assert coordinator.available is True

    with patch(
        "custom_components.opendisplay.coordinator._utc_timestamp",
        return_value=1721,
    ):
        assert coordinator.available is False


def test_deep_sleep_deadline_schedules_state_update() -> None:
    """Restored last_seen should schedule a HA state update at the sleep deadline."""
    hass = MagicMock()
    cancel_deadline = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False
        self._listeners = {}

    with patch(original_init, _capture_init), patch(
        "custom_components.opendisplay.coordinator.async_track_point_in_utc_time",
        return_value=cancel_deadline,
    ) as mock_track, patch(
        "custom_components.opendisplay.coordinator._utc_timestamp",
        return_value=1000.0,
    ):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=120,
        )
        coordinator.async_restore_last_seen(
            datetime.fromtimestamp(900, tz=timezone.utc)
        )

    mock_track.assert_called_once()
    assert mock_track.call_args.args[0] is hass
    assert mock_track.call_args.args[2].timestamp() == 1440.0
    assert coordinator._deep_sleep_deadline_unsub is cancel_deadline


def test_deep_sleep_deadline_updates_listeners_when_window_expires() -> None:
    """Entities should be refreshed when the deep-sleep window expires."""
    hass = MagicMock()
    listener = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = True
        self._listeners = {object(): (listener, None)}

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=120,
        )

    coordinator.data = SimpleNamespace(last_seen=1000.0)
    with patch(
        "custom_components.opendisplay.coordinator._utc_timestamp",
        return_value=1540.0,
    ):
        coordinator._async_deep_sleep_deadline_reached(
            datetime.fromtimestamp(1540, tz=timezone.utc)
        )

    assert coordinator.available is False
    assert coordinator._available is False
    listener.assert_called_once()


def test_non_opendisplay_advertisement_preserves_restored_last_seen() -> None:
    """Non-OpenDisplay advertisements should not erase restored sleep state."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False
        self._listeners = {}

    with patch(original_init, _capture_init), patch(
        "custom_components.opendisplay.coordinator.async_track_point_in_utc_time",
        return_value=MagicMock(),
    ), patch(
        "custom_components.opendisplay.coordinator._utc_timestamp",
        return_value=1000.0,
    ):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=120,
        )
        coordinator.async_restore_last_seen(
            datetime.fromtimestamp(900, tz=timezone.utc)
        )

    service_info = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        time=1001.0,
        manufacturer_data={},
    )

    with patch(
        "homeassistant.components.bluetooth.passive_update_coordinator."
        "PassiveBluetoothDataUpdateCoordinator._async_handle_bluetooth_event"
    ) as mock_super:
        coordinator._async_handle_bluetooth_event(
            service_info,
            BluetoothChange.ADVERTISEMENT,
        )

    mock_super.assert_not_called()
    assert coordinator._restored_last_seen == 900.0
    assert coordinator._last_service_info_time is None


def test_expected_wakeup_timestamp_uses_startup_cache_reference() -> None:
    """Expected wake-up should be known even before the first advertisement."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=300,
        )

    with patch(
        "custom_components.opendisplay.coordinator._utc_timestamp",
        return_value=1000,
    ):
        coordinator.async_startup_from_cache()

    expected = coordinator.expected_wakeup_timestamp
    assert expected is not None
    assert expected.timestamp() == 1300.0


def test_fresh_restored_last_seen_tightens_startup_cache_window() -> None:
    """Fresh restored last_seen should override the conservative startup reference."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False
        self._listeners = {}

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=300,
        )

    with patch(
        "custom_components.opendisplay.coordinator._utc_timestamp",
        return_value=1000,
    ):
        coordinator.async_startup_from_cache()
        coordinator.async_restore_last_seen(
            datetime.fromtimestamp(400, tz=timezone.utc)
        )
        assert coordinator.available is True

    deadline = coordinator.deep_sleep_availability_deadline_timestamp
    assert deadline is not None
    assert deadline.timestamp() == 1120.0


def test_stale_restored_last_seen_aligns_to_current_startup_cycle() -> None:
    """Stale restored last_seen should align to the current sleep cycle."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False
        self._listeners = {}

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=300,
        )

    with patch(
        "custom_components.opendisplay.coordinator._utc_timestamp",
        return_value=1000,
    ):
        coordinator.async_startup_from_cache()
        coordinator.async_restore_last_seen(
            datetime.fromtimestamp(200, tz=timezone.utc)
        )
        assert coordinator.available is True

    assert coordinator._restored_last_seen == 500.0
    expected = coordinator.expected_wakeup_timestamp
    assert expected is not None
    assert expected.timestamp() == 800.0
    deadline = coordinator.deep_sleep_availability_deadline_timestamp
    assert deadline is not None
    assert deadline.timestamp() == 1220.0


def test_restored_last_seen_aligns_to_next_deep_sleep_wake_cycle() -> None:
    """Restart during sleep should keep device available until next wake margin."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False
        self._listeners = {}

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=600,
            deep_sleep_timeout_margin_minutes=6,
        )

    with patch(
        "custom_components.opendisplay.coordinator._utc_timestamp",
        return_value=1000 + (19 * 60),
    ):
        coordinator.async_startup_from_cache()
        coordinator.async_restore_last_seen(
            datetime.fromtimestamp(1000, tz=timezone.utc)
        )
        assert coordinator.available is True

    assert coordinator._restored_last_seen == 1000 + (10 * 60)
    expected = coordinator.expected_wakeup_timestamp
    assert expected is not None
    assert expected.timestamp() == 1000 + (20 * 60)
    deadline = coordinator.deep_sleep_availability_deadline_timestamp
    assert deadline is not None
    assert deadline.timestamp() == 1000 + (26 * 60)


def test_expected_wakeup_timestamp_uses_restored_last_seen() -> None:
    """Expected wake-up should use restored last_seen before fresh data arrives."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False
        self._listeners = {}

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            deep_sleep_time_seconds=300,
        )

    coordinator.async_restore_last_seen(datetime.fromtimestamp(600, tz=timezone.utc))
    expected = coordinator.expected_wakeup_timestamp
    assert expected is not None
    assert expected.timestamp() == 900.0


def test_coordinator_keeps_parsed_update_in_data_after_super_call() -> None:
    """Coordinator data should remain OpenDisplayUpdate, not raw service info."""
    hass = MagicMock()
    original_init = (
        "homeassistant.components.bluetooth.passive_update_coordinator"
        ".PassiveBluetoothDataUpdateCoordinator.__init__"
    )

    def _capture_init(self, *args, **kwargs):
        self._LOGGER = MagicMock()
        self.hass = hass
        self.address = args[2] if len(args) > 2 else kwargs.get("address")
        self._available = False
        self._listeners = {}

    with patch(original_init, _capture_init):
        coordinator = OpenDisplayCoordinator(hass, "AA:BB:CC:DD:EE:FF")

    coordinator._started_ble_time = 1000.0
    service_info = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        time=1001.0,
        rssi=-60,
        manufacturer_data={0x2A8A: b"raw"},
    )

    with patch(
        "custom_components.opendisplay.coordinator.MANUFACTURER_ID",
        0x2A8A,
    ), patch(
        "custom_components.opendisplay.coordinator.parse_advertisement",
        return_value=SimpleNamespace(),
    ), patch(
        "homeassistant.components.bluetooth.passive_update_coordinator."
        "PassiveBluetoothDataUpdateCoordinator._async_handle_bluetooth_event"
    ) as mock_super:
        mock_super.side_effect = (
            lambda svc, _chg: setattr(coordinator, "data", svc)
        )
        with patch(
            "custom_components.opendisplay.coordinator._utc_timestamp",
            return_value=1700000000.0,
        ):
            coordinator._async_handle_bluetooth_event(
                service_info,
                BluetoothChange.ADVERTISEMENT,
            )

    assert isinstance(coordinator.data, OpenDisplayUpdate)
    assert coordinator.data.last_seen == 1700000000.0
    assert coordinator.data.last_seen_ble_time == 1001.0
