"""Integration for OpenDisplay BLE e-paper displays."""

import asyncio
import contextlib
from dataclasses import dataclass, field
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
from opendisplay.partial import PartialState

from homeassistant.components.bluetooth import (
    BluetoothReachabilityIntent,
    async_address_reachability_diagnostics,
    async_ble_device_from_address,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.typing import ConfigType

if TYPE_CHECKING:
    from opendisplay.models import FirmwareVersion

from .const import CONF_ENCRYPTION_KEY, DOMAIN
from .coordinator import OpenDisplayCoordinator
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
    partial_state: PartialState = field(default_factory=PartialState)


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
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={
                "address": address,
                "reason": async_address_reachability_diagnostics(
                    hass,
                    address.upper(),
                    BluetoothReachabilityIntent.CONNECTION,
                ),
            },
        )
    encryption_key = _get_encryption_key(entry)

    try:
        async with OpenDisplayDevice(
            mac_address=address, ble_device=ble_device, encryption_key=encryption_key
        ) as device:
            fw = await device.read_firmware_version()
            is_flex = device.is_flex
            # Capture while connected: landing_url() reads the advertised name.
            landing_url = device.landing_url()
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
        hw_version=f"{manufacturer.board_type_name or manufacturer.board_type}"
        if is_flex
        else None,
        configuration_url=landing_url,
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

    @callback
    def _schedule_reboot_reload() -> None:
        """Re-read firmware/config after the device signals a reboot."""
        hass.async_create_task(_async_reload_after_reboot(hass, entry))

    entry.async_on_unload(coordinator.async_subscribe_reboot(_schedule_reboot_reload))

    return True


def _get_platforms(runtime_data: OpenDisplayRuntimeData) -> list[Platform]:
    """Return the platforms to set up for this device."""
    platforms = list(_FLEX_PLATFORMS if runtime_data.is_flex else _BASE_PLATFORMS)
    if not runtime_data.is_flex and runtime_data.device_config.touch_controllers:
        platforms.append(Platform.EVENT)
    return platforms


async def _async_reload_after_reboot(
    hass: HomeAssistant, entry: OpenDisplayConfigEntry
) -> None:
    """Re-read firmware/config after a device reboot by reloading the entry.

    Triggered by the coordinator when the advertised reboot flag goes
    False -> True. Reloading re-runs async_setup_entry, which reconnects (clearing
    the device's reboot flag), re-reads firmware + config, and rebuilds device
    info and platforms. Defers until any in-progress image upload finishes so an
    unrelated reboot detection does not abort the user's upload.
    """
    runtime = entry.runtime_data
    upload_task = runtime.upload_task if runtime is not None else None
    if upload_task is not None and not upload_task.done():
        await asyncio.gather(upload_task, return_exceptions=True)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: OpenDisplayConfigEntry
) -> bool:
    """Unload a config entry."""
    if (task := entry.runtime_data.upload_task) and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return await hass.config_entries.async_unload_platforms(
        entry, _get_platforms(entry.runtime_data)
    )
