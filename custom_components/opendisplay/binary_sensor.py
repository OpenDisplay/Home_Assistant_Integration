"""Binary sensor platform for OpenDisplay devices."""

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenDisplayConfigEntry
from .const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TLS,
    DEFAULT_LAN_PORT,
    SIGNAL_PENDING_STATE,
)
from .delivery import DeliveryManager, DeliverySnapshot
from .entity import OpenDisplayEntity

PARALLEL_UPDATES = 0

_CONNECTIVITY_DESCRIPTION = BinarySensorEntityDescription(
    key="connectivity",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the OpenDisplay binary sensors."""
    entities: list[BinarySensorEntity] = [
        OpenDisplayConnectivityBinarySensor(
            entry.runtime_data.coordinator, _CONNECTIVITY_DESCRIPTION
        )
    ]

    manager = entry.runtime_data.delivery
    if manager is not None:
        entities.append(OpenDisplayUpdatePendingSensor(entry.unique_id or "", manager))

    # WiFi (LAN) status. Only for WiFi-capable/-configured tags (device carries a
    # wifi_config packet, or a LAN host is already stored) so pure-BLE devices
    # don't get a permanently-off entity.
    if entry.runtime_data.device_config.wifi_config is not None or entry.data.get(
        CONF_HOST
    ):
        entities.append(OpenDisplayWifiSensor(entry))

    async_add_entities(entities)


def _to_iso(epoch: float | None) -> str | None:
    """Convert an epoch timestamp to an ISO string, or None."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


class OpenDisplayConnectivityBinarySensor(
    OpenDisplayEntity[BinarySensorEntityDescription], BinarySensorEntity
):
    """Reports whether the OpenDisplay device is currently advertising over BLE.

    For a deep-sleeping tag this follows the coordinator's availability, which
    the fallback availability interval keeps up across expected sleep cycles,
    so it only turns off once the tag has missed its wakes.
    """

    @property
    def available(self) -> bool:
        """Connectivity is reported regardless of the device's availability."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True if the device is currently reachable via BLE."""
        return self.coordinator.available


class OpenDisplayUpdatePendingSensor(BinarySensorEntity):
    """On while content is queued for delivery at the next wake.

    Backed entirely by the delivery manager's state, so it stays available and
    meaningful even while the device is dark.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "update_pending"

    def __init__(self, address: str, manager: DeliveryManager) -> None:
        """Initialize the binary sensor from the manager's current state."""
        self._address = address
        self._attr_unique_id = f"{address}-update_pending"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
        )
        self._apply(manager.state)

    @callback
    def _apply(self, snapshot: DeliverySnapshot) -> None:
        """Store the latest delivery snapshot on the entity."""
        self._attr_is_on = snapshot.pending
        self._queued_at = snapshot.queued_at
        self._expires_at = snapshot.expires_at
        self._attempts = snapshot.attempts
        self._last_error = snapshot.last_error
        self._auth_paused = snapshot.auth_paused

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose queue timing details for automations/dashboards."""
        return {
            "queued_at": _to_iso(self._queued_at),
            "expires_at": _to_iso(self._expires_at),
            "attempts": self._attempts,
            "last_error": self._last_error,
            # Authoritative: an expiring slot can overwrite last_error with
            # "expired" while delivery is still blocked on authentication.
            "auth_paused": self._auth_paused,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to delivery state updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_PENDING_STATE}_{self._address}",
                self._handle_pending_state,
            )
        )

    @callback
    def _handle_pending_state(self, snapshot: DeliverySnapshot) -> None:
        """Handle a delivery-state change."""
        self._apply(snapshot)
        self.async_write_ha_state()


class OpenDisplayWifiSensor(BinarySensorEntity):
    """Whether the WiFi (LAN) transport is enabled for an OpenDisplay device.

    On when the integration holds a LAN endpoint (IP) for this tag — learned
    from the device's mDNS announcement and stored on the config entry. The IP,
    port and TLS flag are surfaced as attributes so the device page shows where
    WiFi delivery connects. Backed entirely by config-entry data, so it stays
    available and meaningful while the device is asleep. The config entry is
    reloaded whenever mDNS updates the host, which recreates this entity, so
    reading ``entry.data`` on each access always reflects the current endpoint.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "wifi"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: OpenDisplayConfigEntry) -> None:
        """Initialize from the config entry that stores the LAN endpoint."""
        self._entry = entry
        address = entry.unique_id or ""
        self._attr_unique_id = f"{address}-wifi"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
        )

    @property
    def is_on(self) -> bool:
        """True when a LAN endpoint (IP) is configured for this device."""
        return bool(self._entry.data.get(CONF_HOST))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the LAN endpoint while WiFi is enabled."""
        host = self._entry.data.get(CONF_HOST)
        if not host:
            return {}
        return {
            "ip_address": host,
            "port": int(self._entry.data.get(CONF_PORT) or DEFAULT_LAN_PORT),
            "tls": bool(self._entry.data.get(CONF_TLS)),
        }
