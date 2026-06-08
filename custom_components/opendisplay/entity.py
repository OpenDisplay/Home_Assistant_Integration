"""Base entity for OpenDisplay devices."""

from typing import Generic, TypeVar

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import EntityDescription

from .coordinator import OpenDisplayCoordinator

_DescriptionT = TypeVar("_DescriptionT", bound=EntityDescription)


class OpenDisplayEntity(
    PassiveBluetoothCoordinatorEntity[OpenDisplayCoordinator],
    Generic[_DescriptionT],
):
    """Base class for all OpenDisplay entities."""

    _attr_has_entity_name = True
    _attr_assumed_state = False
    entity_description: _DescriptionT

    def __init__(
        self,
        coordinator: OpenDisplayCoordinator,
        description: _DescriptionT,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}-{description.key}"

        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
        )

    @property
    def available(self) -> bool:
        """Return True when coordinator reports device available."""
        return self.coordinator.available

    @property
    def assumed_state(self) -> bool:
        """OpenDisplay entities do not expose assumed state."""
        return False
