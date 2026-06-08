"""Tests for pending upload diagnostic binary sensor."""

from types import SimpleNamespace
from unittest.mock import patch

from custom_components.opendisplay.binary_sensor import (
    OpenDisplayPendingUploadBinarySensorEntity,
)


def _make_coordinator(pending_upload: bool):
    runtime_data = SimpleNamespace(
        device_config=SimpleNamespace(
            power=SimpleNamespace(deep_sleep_time_seconds=300)
        )
    )
    config_entry = SimpleNamespace(runtime_data=runtime_data)
    return SimpleNamespace(
        available=True,
        pending_upload=pending_upload,
        address="AA:BB:CC:DD:EE:FF",
        config_entry=config_entry,
    )


def test_pending_upload_binary_sensor_reflects_coordinator_state() -> None:
    coordinator = _make_coordinator(pending_upload=True)
    description = SimpleNamespace(key="pending_upload")

    with patch(
        "custom_components.opendisplay.entity.PassiveBluetoothCoordinatorEntity.__init__",
        lambda self, coordinator: setattr(self, "coordinator", coordinator),
    ):
        entity = OpenDisplayPendingUploadBinarySensorEntity(coordinator, description)

    assert entity.is_on is True

    coordinator.pending_upload = False
    assert entity.is_on is False
