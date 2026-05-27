"""Integration for OpenDisplay BLE e-paper displays."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opendisplay import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    GlobalConfig,
    OpenDisplayDevice,
    OpenDisplayError,
)

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.typing import ConfigType

if TYPE_CHECKING:
    from opendisplay.models import FirmwareVersion

from .const import (
    CONF_ENCRYPTION_KEY,
    DOMAIN,
)
from .coordinator import OpenDisplayCoordinator
from .deep_sleep import DeepSleepQueuedUpload
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_BASE_PLATFORMS: list[Platform] = [Platform.IMAGE, Platform.SENSOR]
_FLEX_PLATFORMS = [Platform.EVENT, Platform.IMAGE, Platform.SENSOR, Platform.UPDATE]


@dataclass
class OpenDisplayRuntimeData:
    """Runtime data for an OpenDisplay config entry."""

    coordinator: OpenDisplayCoordinator
    firmware: FirmwareVersion
    device_config: GlobalConfig
    is_flex: bool
    upload_task: asyncio.Task | None = None
    deep_sleep_upload: DeepSleepQueuedUpload | None = None
    deep_sleep_expiry_handle: asyncio.TimerHandle | None = None


type OpenDisplayConfigEntry = ConfigEntry[OpenDisplayRuntimeData]


def _get_encryption_key(entry: OpenDisplayConfigEntry) -> bytes | None:
    """Return the encryption key bytes from entry data, or None."""
    raw = entry.data.get(CONF_ENCRYPTION_KEY)
    if raw is None:
        return None
    if len(raw) != 32:
        raise ConfigEntryAuthFailed(
            "Stored OpenDisplay encryption key is invalid; reauthentication required"
        )
    try:
        return bytes.fromhex(raw)
    except ValueError as err:
        raise ConfigEntryAuthFailed(
            "Stored OpenDisplay encryption key is invalid; reauthentication required"
        ) from err


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the OpenDisplay integration."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OpenDisplayConfigEntry) -> bool:
    """Set up OpenDisplay from a config entry."""
    address = entry.unique_id
    if TYPE_CHECKING:
        assert address is not None

    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(
            f"Could not find OpenDisplay device with address {address}"
        )

    encryption_key = _get_encryption_key(entry)

    try:
        async with OpenDisplayDevice(
            mac_address=address, ble_device=ble_device, encryption_key=encryption_key
        ) as device:
            fw = await device.read_firmware_version()
            is_flex = device.is_flex
    except (AuthenticationFailedError, AuthenticationRequiredError) as err:
        raise ConfigEntryAuthFailed(
            f"Encryption key rejected by OpenDisplay device: {err}"
        ) from err
    except (BLEConnectionError, BLETimeoutError, OpenDisplayError) as err:
        raise ConfigEntryNotReady(
            f"Failed to connect to OpenDisplay device: {err}"
        ) from err
    device_config = device.config
    if TYPE_CHECKING:
        assert device_config is not None

    coordinator = OpenDisplayCoordinator(hass, address)

    manufacturer = device_config.manufacturer
    display = device_config.displays[0]
    color_scheme_enum = display.color_scheme_enum
    color_scheme = (
        str(color_scheme_enum)
        if isinstance(color_scheme_enum, int)
        else color_scheme_enum.name
    )
    size = (
        f'{display.screen_diagonal_inches:.1f}"'
        if display.screen_diagonal_inches is not None
        else f"{display.pixel_width}x{display.pixel_height}"
    )
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(CONNECTION_BLUETOOTH, address)},
        manufacturer=manufacturer.manufacturer_name,
        model=f"{size} {color_scheme}",
        sw_version=f"{fw['major']}.{fw['minor']}",
        hw_version=(
            f"{manufacturer.board_type_name or manufacturer.board_type}"
            f" rev. {manufacturer.board_revision}"
        )
        if is_flex
        else None,
        configuration_url="https://opendisplay.org/firmware/config/"
        if is_flex
        else None,
    )

    entry.runtime_data = OpenDisplayRuntimeData(
        coordinator=coordinator,
        firmware=fw,
        device_config=device_config,
        is_flex=is_flex,
    )

    await hass.config_entries.async_forward_entry_setups(
        entry, _get_platforms(entry.runtime_data)
    )
    entry.async_on_unload(coordinator.async_start())

    # Register coordinator listener to flush queued deep-sleep uploads when
    # the device wakes up and becomes connectable again.
    def _on_coordinator_update() -> None:
        """Try to flush any queued deep-sleep upload when device advertises."""
        queued = entry.runtime_data.deep_sleep_upload
        if queued is None:
            return
        if queued.is_expired:
            entry.runtime_data.deep_sleep_upload = None
            if (handle := entry.runtime_data.deep_sleep_expiry_handle) is not None:
                handle.cancel()
                entry.runtime_data.deep_sleep_expiry_handle = None
            return
        if async_ble_device_from_address(hass, address, connectable=True) is None:
            return
        # Device is now connectable – flush the queued upload
        entry.runtime_data.deep_sleep_upload = None
        if (handle := entry.runtime_data.deep_sleep_expiry_handle) is not None:
            handle.cancel()
            entry.runtime_data.deep_sleep_expiry_handle = None
        from .services import _async_connect_and_run  # noqa: PLC0415 – avoid circular import at module level
        hass.async_create_task(
            _async_connect_and_run(hass, entry, queued.action),
            name=f"opendisplay_deepsleep_flush_{address}",
        )

    entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))

    return True


def _get_platforms(runtime_data: OpenDisplayRuntimeData) -> list[Platform]:
    """Return the platforms to set up for this device."""
    platforms = list(_FLEX_PLATFORMS if runtime_data.is_flex else _BASE_PLATFORMS)
    if not runtime_data.is_flex and runtime_data.device_config.touch_controllers:
        platforms.append(Platform.EVENT)
    return platforms


async def async_unload_entry(
    hass: HomeAssistant, entry: OpenDisplayConfigEntry
) -> bool:
    """Unload a config entry."""
    if (handle := entry.runtime_data.deep_sleep_expiry_handle) is not None:
        handle.cancel()
        entry.runtime_data.deep_sleep_expiry_handle = None

    if (task := entry.runtime_data.upload_task) and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return await hass.config_entries.async_unload_platforms(
        entry, _get_platforms(entry.runtime_data)
    )
