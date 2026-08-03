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
from .delivery import DeliverySnapshot
from .storage import OpenDisplayContentStore

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the OpenDisplay image entity."""
    async_add_entities(
        [OpenDisplayImageEntity(hass, entry)]
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

    def __init__(self, hass: HomeAssistant, entry: OpenDisplayConfigEntry) -> None:
        """Initialize the image entity."""
        super().__init__(hass)
        coordinator = entry.runtime_data.coordinator
        self._coordinator = coordinator
        self._store: OpenDisplayContentStore | None = entry.runtime_data.content_store
        self._attr_unique_id = f"{coordinator.address}-display_content"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
        )
        stored = self._store.content if self._store is not None else None
        self._image_bytes: bytes | None = stored.image_jpeg if stored else None
        # When True the shown frame is queued for the next wake, not yet on the
        # panel (D6).
        self._pending: bool = stored.pending if stored else False
        self._queued_at: float | None = stored.queued_at if stored else None
        self._expires_at: float | None = stored.expires_at if stored else None
        self._attempts: int = stored.attempts if stored else 0
        self._last_error: str | None = stored.last_error if stored else None
        if stored and stored.image_last_updated is not None:
            self._attr_image_last_updated = datetime.fromtimestamp(
                stored.image_last_updated, tz=timezone.utc
            )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose whether the shown frame is still waiting to be delivered."""
        return {
            "pending": self._pending,
            "queued_at": _to_iso(self._queued_at),
            "last_error": self._last_error,
        }

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

    @callback
    def _handle_image_update(self, image_bytes: bytes) -> None:
        """Handle a new image from a completed or queued upload."""
        self._image_bytes = image_bytes
        self._attr_image_last_updated = dt_util.utcnow()
        self._store_content()
        self.async_write_ha_state()

    @callback
    def _handle_pending_state(self, snapshot: DeliverySnapshot) -> None:
        """Reflect the delivery manager's pending state on the entity."""
        self._pending = snapshot.pending
        self._queued_at = snapshot.queued_at
        self._expires_at = snapshot.expires_at
        self._attempts = snapshot.attempts
        self._last_error = snapshot.last_error
        self._store_content()
        self.async_write_ha_state()

    @callback
    def _store_content(self) -> None:
        """Persist the current image entity state."""
        if self._store is None or self._image_bytes is None:
            return
        last_updated = self._attr_image_last_updated
        self._store.store_content(
            self._image_bytes,
            image_last_updated=last_updated.timestamp()
            if last_updated is not None
            else datetime.now(tz=timezone.utc).timestamp(),
            pending=self._pending,
            queued_at=self._queued_at,
            expires_at=self._expires_at,
            attempts=self._attempts,
            last_error=self._last_error,
        )
