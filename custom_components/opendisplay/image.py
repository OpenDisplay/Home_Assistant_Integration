"""Image entity for OpenDisplay devices."""

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
import homeassistant.util.dt as dt_util

from . import OpenDisplayConfigEntry
from .const import SIGNAL_IMAGE_UPDATED
from .coordinator import OpenDisplayCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the OpenDisplay image entity."""
    async_add_entities([OpenDisplayImageEntity(hass, entry.runtime_data.coordinator)])


class OpenDisplayImageEntity(ImageEntity):
    """Shows the last image sent to an OpenDisplay device."""

    _attr_has_entity_name = True
    _attr_translation_key = "content"
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: OpenDisplayCoordinator,
    ) -> None:
        """Initialize the image entity."""
        super().__init__(hass)
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}-display_content"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
        )
        self._image_bytes: bytes | None = None

    async def async_image(self) -> bytes | None:
        """Return the last uploaded image bytes."""
        return self._image_bytes

    async def async_added_to_hass(self) -> None:
        """Subscribe to image update signals."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_IMAGE_UPDATED}_{self._coordinator.address}",
                self._handle_image_update,
            )
        )

    @callback
    def _handle_image_update(self, image_bytes: bytes) -> None:
        """Handle a new image from a completed upload."""
        self._image_bytes = image_bytes
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()
