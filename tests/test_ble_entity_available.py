"""Tests for OpenDisplayBLEEntity.available property.

Verifies that the entity uses connectable=False when checking
BLE advertisement presence, so that non-connectable advertisements
(e.g. from a device waking from deep sleep) correctly mark the
entity as available.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.opendisplay.entity import OpenDisplayBLEEntity


def _make_entity(mac: str = "AA:BB:CC:DD:EE:FF") -> OpenDisplayBLEEntity:
    """Create a minimal OpenDisplayBLEEntity with mocked dependencies."""
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            device_metadata={
                "model_name": "Test",
                "fw_version": "1.0",
                "width": 296,
                "height": 128,
            }
        )
    )
    entity = OpenDisplayBLEEntity.__new__(OpenDisplayBLEEntity)
    entity._mac_address = mac
    entity._name = "Test Device"
    entity._entry = entry
    entity.hass = MagicMock()
    return entity


def test_available_true_when_non_connectable_advertisement_present() -> None:
    """Entity is available when device is seen via a non-connectable advertisement."""
    entity = _make_entity()

    with patch(
        "custom_components.opendisplay.entity.bluetooth.async_address_present",
        return_value=True,
    ) as mock_present:
        result = entity.available

    assert result is True
    mock_present.assert_called_once_with(
        entity.hass, "AA:BB:CC:DD:EE:FF", connectable=False
    )


def test_available_false_when_device_not_seen() -> None:
    """Entity is unavailable when no advertisement (connectable or not) is present."""
    entity = _make_entity()

    with patch(
        "custom_components.opendisplay.entity.bluetooth.async_address_present",
        return_value=False,
    ) as mock_present:
        result = entity.available

    assert result is False
    mock_present.assert_called_once_with(
        entity.hass, "AA:BB:CC:DD:EE:FF", connectable=False
    )


def test_available_does_not_require_connectable_advertisement() -> None:
    """Passing connectable=True would miss non-connectable deep-sleep wakeups.

    This test documents the fix: we must NOT call async_address_present with
    connectable=True (or its default, which is True), because devices waking
    from deep sleep broadcast non-connectable advertisements.
    """
    entity = _make_entity()

    # Simulate a device that is present only as non-connectable
    def _mock_present(hass, address, connectable=True):
        # Only visible when scanning includes non-connectable devices
        if connectable is False:
            return True
        return False

    with patch(
        "custom_components.opendisplay.entity.bluetooth.async_address_present",
        side_effect=_mock_present,
    ):
        assert entity.available is True, (
            "Entity should be available when device broadcasts a non-connectable "
            "advertisement (e.g. after waking from deep sleep)"
        )
