"""Tests for OpenDisplayCoordinator connectable=False fix.

Verifies that the coordinator uses connectable=False so that non-connectable
advertisements (e.g. from a device waking from deep sleep) are processed and
the entity is correctly reported as available.
"""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.opendisplay.coordinator import (
    BluetoothScanningMode,
    OpenDisplayCoordinator,
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
