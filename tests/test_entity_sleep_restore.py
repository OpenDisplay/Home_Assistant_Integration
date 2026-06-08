"""Tests for sensor restore behavior and coordinator-driven availability."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.opendisplay.entity import OpenDisplayEntity
from custom_components.opendisplay.sensor import (
    OpenDisplaySensorEntity,
    OpenDisplaySensorEntityDescription,
)

_COORDINATOR_ENTITY_INIT = (
    "custom_components.opendisplay.entity."
    "PassiveBluetoothCoordinatorEntity.__init__"
)


def _make_coordinator(*, available: bool, deep_sleep_seconds: int, data=None):
    """Create a minimal coordinator-like object for entity unit tests."""
    return SimpleNamespace(
        available=available,
        data=data,
        address="AA:BB:CC:DD:EE:FF",
        deep_sleep_time_seconds=deep_sleep_seconds,
        expected_wakeup_timestamp=None,
        async_restore_last_seen=MagicMock(),
    )


def _make_description() -> OpenDisplaySensorEntityDescription:
    """Return a minimal sensor description for tests."""
    return OpenDisplaySensorEntityDescription(
        key="temperature",
        value_fn=lambda upd: upd.advertisement.temperature_c,
    )


def _build_entity(
    *,
    available: bool,
    deep_sleep_seconds: int,
    data=None,
) -> OpenDisplaySensorEntity:
    """Build sensor entity with patched coordinator base initializer."""
    coordinator = _make_coordinator(
        available=available,
        deep_sleep_seconds=deep_sleep_seconds,
        data=data,
    )
    with patch(
        _COORDINATOR_ENTITY_INIT,
        lambda self, coordinator: setattr(self, "coordinator", coordinator),
    ):
        return OpenDisplaySensorEntity(coordinator, _make_description())


def test_sleeping_device_is_unavailable_without_advertisements() -> None:
    """Without coordinator availability the entity remains unavailable."""
    entity = _build_entity(available=False, deep_sleep_seconds=300)

    assert entity.available is False
    assert entity.assumed_state is False


def test_non_sleeping_offline_device_is_unavailable() -> None:
    """Offline device without deep sleep should be unavailable."""
    entity = _build_entity(available=False, deep_sleep_seconds=0)

    assert entity.available is False
    assert entity.assumed_state is False


def test_online_device_not_assumed() -> None:
    """Online devices should never report assumed state."""
    entity = _build_entity(available=True, deep_sleep_seconds=300)

    assert entity.available is True
    assert entity.assumed_state is False


def test_sensor_native_value_restores_last_state_when_sleeping() -> None:
    """When coordinator has no fresh data, sensor falls back to restored value."""
    entity = _build_entity(available=False, deep_sleep_seconds=300)
    entity._attr_native_value = 22.5

    assert entity.native_value == 22.5


def test_sensor_native_value_without_deep_sleep_does_not_restore() -> None:
    """Restore fallback is disabled when device does not support deep sleep."""
    entity = _build_entity(available=False, deep_sleep_seconds=0)
    entity._attr_native_value = 22.5

    assert entity.native_value is None


def test_sensor_native_value_prefers_live_data_over_restored() -> None:
    """Fresh coordinator data must override restored value."""
    data = SimpleNamespace(advertisement=SimpleNamespace(temperature_c=19.8))
    entity = _build_entity(available=True, deep_sleep_seconds=300, data=data)
    entity._attr_native_value = 22.5

    assert entity.native_value == 19.8


async def test_sensor_async_added_to_hass_restores_when_deep_sleep_enabled() -> None:
    """RestoreSensor lifecycle should load native value for deep-sleep devices."""
    entity = _build_entity(available=False, deep_sleep_seconds=300)
    last_sensor_data = SimpleNamespace(native_value=22.5)

    with patch.object(
        OpenDisplayEntity,
        "async_added_to_hass",
        new_callable=AsyncMock,
        create=True,
    ), patch.object(
        entity,
        "async_get_last_sensor_data",
        new_callable=AsyncMock,
        return_value=last_sensor_data,
    ) as mock_get_last_sensor_data:
        await entity.async_added_to_hass()

    mock_get_last_sensor_data.assert_awaited_once()
    assert entity.native_value == 22.5


async def test_async_added_to_hass_skips_restore_when_deep_sleep_disabled() -> None:
    """Always-on devices should not reuse stale restored sensor values."""
    entity = _build_entity(available=False, deep_sleep_seconds=0)

    with patch.object(
        OpenDisplayEntity,
        "async_added_to_hass",
        new_callable=AsyncMock,
        create=True,
    ), patch.object(
        entity,
        "async_get_last_sensor_data",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(native_value=22.5),
    ) as mock_get_last_sensor_data:
        await entity.async_added_to_hass()

    mock_get_last_sensor_data.assert_not_awaited()
    assert entity.native_value is None


async def test_last_seen_restore_updates_coordinator_sleep_reference() -> None:
    """Restored last_seen should tighten coordinator wake-up calculations."""
    coordinator = _make_coordinator(
        available=False,
        deep_sleep_seconds=300,
        data=None,
    )
    description = OpenDisplaySensorEntityDescription(
        key="last_seen",
        value_fn=lambda upd: None,
    )
    with patch(
        _COORDINATOR_ENTITY_INIT,
        lambda self, coordinator: setattr(self, "coordinator", coordinator),
    ):
        entity = OpenDisplaySensorEntity(coordinator, description)

    restored = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    with patch.object(
        OpenDisplayEntity,
        "async_added_to_hass",
        new_callable=AsyncMock,
        create=True,
    ), patch.object(
        entity,
        "async_get_last_sensor_data",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(native_value=restored),
    ):
        await entity.async_added_to_hass()

    coordinator.async_restore_last_seen.assert_called_once_with(restored)


def test_expected_wakeup_keeps_restored_value_without_fresh_advertisement() -> None:
    """Expected wakeup should not be erased while the device is still asleep."""
    coordinator = _make_coordinator(
        available=False,
        deep_sleep_seconds=300,
        data=None,
    )
    description = OpenDisplaySensorEntityDescription(
        key="expected_wakeup",
        value_fn=lambda upd: None,
    )
    with patch(
        _COORDINATOR_ENTITY_INIT,
        lambda self, coordinator: setattr(self, "coordinator", coordinator),
    ):
        entity = OpenDisplaySensorEntity(coordinator, description)

    restored = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    entity._attr_native_value = restored

    assert entity.native_value == restored

    live = datetime(2026, 6, 1, 8, 5, tzinfo=timezone.utc)
    coordinator.expected_wakeup_timestamp = live
    assert entity.native_value == live
