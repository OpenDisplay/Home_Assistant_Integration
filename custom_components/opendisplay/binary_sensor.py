"""Binary sensor platform for OpenDisplay diagnostic entities."""

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenDisplayConfigEntry
from .entity import OpenDisplayEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class OpenDisplayBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes an OpenDisplay binary sensor entity."""


_PENDING_UPLOAD_DESCRIPTION = OpenDisplayBinarySensorEntityDescription(
    key="pending_upload",
    translation_key="pending_upload",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up OpenDisplay binary sensor entities."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            OpenDisplayPendingUploadBinarySensorEntity(
                coordinator,
                _PENDING_UPLOAD_DESCRIPTION,
            )
        ]
    )


class OpenDisplayPendingUploadBinarySensorEntity(
    OpenDisplayEntity[OpenDisplayBinarySensorEntityDescription],
    BinarySensorEntity,
):
    """Binary sensor representing pending upload queue state."""

    entity_description: OpenDisplayBinarySensorEntityDescription

    @property
    def is_on(self) -> bool:
        """Return True when there is a pending upload to be sent."""
        return self.coordinator.pending_upload
