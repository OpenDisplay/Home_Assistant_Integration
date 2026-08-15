"""Image entity for OpenDisplay devices."""

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
import homeassistant.util.dt as dt_util

from . import OpenDisplayConfigEntry
from .const import SIGNAL_IMAGE_UPDATED, SIGNAL_PENDING_STATE
from .coordinator import OpenDisplayCoordinator
from .delivery import DeliverySnapshot
from .designer.image_entity import (
    designer_extra_state_attributes,
    designer_on_entity_added,
)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the OpenDisplay image entity."""
    async_add_entities(
        [OpenDisplayImageEntity(hass, entry.runtime_data.coordinator, entry)]
    )


def _to_iso(epoch: float | None) -> str | None:
    """Convert an epoch timestamp to an ISO string, or None."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class OpenDisplayImageEntity(ImageEntity):
    """Shows the last image sent to (or queued for) an OpenDisplay device."""

    _attr_has_entity_name = True
    _attr_translation_key = "content"
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: OpenDisplayCoordinator,
        entry: OpenDisplayConfigEntry | None = None,
    ) -> None:
        """Initialize the image entity."""
        super().__init__(hass)
        self._coordinator = coordinator
        self._designer_entry = entry
        self._attr_unique_id = f"{coordinator.address}-display_content"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
        )
        self._image_bytes: bytes | None = None
        # When True the shown frame is queued for the next wake, not yet on the
        # panel (D6).
        self._pending: bool = False
        self._queued_at: float | None = None
        self._last_error: str | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose delivery state and designer display capabilities."""
        attrs: dict[str, Any] = {
            "pending": self._pending,
            "queued_at": _to_iso(self._queued_at),
            "last_error": self._last_error,
        }
        attrs.update(designer_extra_state_attributes(self, self._designer_entry))
        return attrs

    async def async_image(self) -> bytes | None:
        """Return the last uploaded (or queued) image bytes."""
        return self._image_bytes

    async def async_added_to_hass(self) -> None:
        """Subscribe to image and pending-state update signals."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_IMAGE_UPDATED}_{self._coordinator.address}",
                self._handle_image_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_PENDING_STATE}_{self._coordinator.address}",
                self._handle_pending_state,
            )
        )
        await designer_on_entity_added(self, self._designer_entry)

    @callback
    def _handle_image_update(self, image_bytes: bytes) -> None:
        """Handle a new image from a completed or queued upload."""
        self._image_bytes = image_bytes
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    @callback
    def _handle_pending_state(self, snapshot: DeliverySnapshot) -> None:
        """Reflect the delivery manager's pending state on the entity."""
        self._pending = snapshot.pending
        self._queued_at = snapshot.queued_at
        self._last_error = snapshot.last_error
        self.async_write_ha_state()
