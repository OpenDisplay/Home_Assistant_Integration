"""Integration for OpenDisplay BLE e-paper displays."""

import asyncio
import contextlib
from dataclasses import dataclass, field
import logging
import time
from typing import TYPE_CHECKING, Any

from opendisplay import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    GlobalConfig,
    OpenDisplayDevice,
    OpenDisplayError,
    PartialState,
)
from opendisplay.models.config_json import config_from_json, config_to_json

from homeassistant.components.bluetooth import (
    BluetoothReachabilityIntent,
    async_address_reachability_diagnostics,
    async_ble_device_from_address,
    async_set_fallback_availability_interval,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.typing import ConfigType

from .ble_lock import async_get_ble_lock, ble_connection
from .const import CONF_CACHED_STATE, CONF_ENCRYPTION_KEY, DOMAIN, SETUP_DEADLINE_S
from .coordinator import OpenDisplayCoordinator
from .delivery import DeliveryManager
from .designer import async_setup_designer
from .services import async_setup_services
from .sleep import SleepProfile

if TYPE_CHECKING:
    from opendisplay.models import FirmwareVersion

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_BASE_PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.IMAGE,
    Platform.SENSOR,
]
_FLEX_PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.SENSOR,
    Platform.UPDATE,
]


@dataclass
class OpenDisplayRuntimeData:
    """Runtime data for an OpenDisplay config entry."""

    coordinator: OpenDisplayCoordinator
    firmware: FirmwareVersion
    device_config: GlobalConfig
    is_flex: bool
    # Resolved deep-sleep behavior (options + device power config).
    sleep_profile: SleepProfile
    # Serializes every BLE connection to this tag. Process-global per-MAC
    # (ble_lock.py), so the same lock object is shared across every connect site
    # and survives entry reloads. The device exposes a single BLE link with no
    # per-address lock in the library, so drawcustom/upload_image, LED, buzzer
    # and OTA must not open overlapping connections or they race and surface a
    # confusing upload_error. Required (no default) so a future construction
    # can't silently mint a private, non-shared lock and recreate this bug.
    ble_lock: asyncio.Lock
    upload_task: asyncio.Task | None = None
    # Tracks the last uploaded frame + etag for differential partial updates
    # (0x76). Replaced with a fresh instance on every full/fast refresh so the
    # next partial diffs against the frame actually on the panel.
    partial_state: PartialState = field(default_factory=PartialState)
    # Set when the entry was set up from cache without connecting; the delivery
    # manager re-reads firmware/config at the next wake and refreshes the cache.
    config_resync_pending: bool = False
    # Owns queued work delivered at the next wake (set in async_setup_entry).
    delivery: DeliveryManager | None = None
    # Monotonic timestamp of the last mDNS sighting (fed by the zeroconf config
    # step). Drives the WiFi-vs-BLE transport choice (see transport.py); None
    # until the device is first seen via mDNS.
    mdns_last_seen: float | None = None
    # Label of the transport the most recent delivery completed over
    # ("wifi"/"ble"); surfaced in diagnostics. None until a delivery runs.
    last_transport: str | None = None


type OpenDisplayConfigEntry = ConfigEntry[OpenDisplayRuntimeData]


@dataclass
class _CachedState:
    """Device state restored from ``entry.data`` for setup-without-connect."""

    firmware: FirmwareVersion
    is_flex: bool
    device_config: GlobalConfig
    landing_url: str | None


def _load_cache(entry: OpenDisplayConfigEntry) -> _CachedState | None:
    """Rebuild cached device state from the config entry, or None if absent."""
    raw = entry.data.get(CONF_CACHED_STATE)
    if not raw:
        return None
    try:
        device_config = config_from_json(raw["config"])
        return _CachedState(
            firmware=raw["firmware"],
            is_flex=raw["is_flex"],
            device_config=device_config,
            landing_url=raw.get("landing_url"),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _write_cache(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    device_config: GlobalConfig,
    firmware: FirmwareVersion,
    is_flex: bool,
    landing_url: str | None,
) -> None:
    """Persist the device state needed to set up the entry without connecting.

    Called after every successful interrogation (setup and, later, wake-time
    resync). Writes only when the meaningful contents changed so we don't churn
    the config-entry store on every reload.
    """
    payload: dict[str, Any] = {
        "config": config_to_json(device_config),
        "firmware": dict(firmware),
        "is_flex": is_flex,
        "landing_url": landing_url,
    }
    existing = entry.data.get(CONF_CACHED_STATE)
    if existing is not None and all(
        existing.get(key) == value for key, value in payload.items()
    ):
        return
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_CACHED_STATE: {**payload, "cached_at": time.time()}},
    )


def _cache_setup_if_sleepy(
    entry: OpenDisplayConfigEntry,
) -> _CachedState | None:
    """Return cached state iff it exists and resolves to a sleepy device."""
    cached = _load_cache(entry)
    if cached is None:
        return None
    profile = SleepProfile.from_entry(entry, cached.device_config)
    if not profile.is_sleepy:
        return None
    return cached


def _get_encryption_key(entry: OpenDisplayConfigEntry) -> bytes | None:
    """Return the encryption key bytes from entry data, or None."""
    raw = entry.data.get(CONF_ENCRYPTION_KEY)
    if raw is None:
        return None
    if len(raw) != 32:
        _LOGGER.error(
            "%s: stored encryption key is malformed (bad length); reauthentication required",
            entry.unique_id,
        )
        raise ConfigEntryAuthFailed(
            "Stored OpenDisplay encryption key is invalid; reauthentication required"
        )
    try:
        return bytes.fromhex(raw)
    except ValueError as err:
        _LOGGER.error(
            "%s: stored encryption key is malformed (not hex); reauthentication required",
            entry.unique_id,
        )
        raise ConfigEntryAuthFailed(
            "Stored OpenDisplay encryption key is invalid; reauthentication required"
        ) from err


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the OpenDisplay integration."""
    async_setup_services(hass)
    await async_setup_designer(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OpenDisplayConfigEntry) -> bool:
    """Set up OpenDisplay from a config entry.

    A reachable device is interrogated live (firmware + config) and the result
    cached. When the device is dark and the cache says it is a deep-sleeping
    tag, the entry is set up entirely from cache without connecting; the
    delivery manager re-reads it at the next wake. Any other unreachable case
    preserves the original ConfigEntryNotReady/ConfigEntryAuthFailed behavior.
    """
    address = entry.unique_id
    if TYPE_CHECKING:
        assert address is not None

    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    from_cache = False

    if ble_device is None:
        cached = _cache_setup_if_sleepy(entry)
        if cached is None:
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
        fw = cached.firmware
        is_flex = cached.is_flex
        device_config = cached.device_config
        landing_url = cached.landing_url
        from_cache = True
    else:
        encryption_key = _get_encryption_key(entry)
        try:
            # Bound the active connect (connect + interrogate + firmware read) so a
            # wedged BLE link can't stall setup forever; a breach is treated like
            # any other connect failure below (sleepy-cache fallback / retry).
            async with asyncio.timeout(SETUP_DEADLINE_S):
                async with ble_connection(address, "setup interrogation"), OpenDisplayDevice(
                    mac_address=address,
                    ble_device=ble_device,
                    encryption_key=encryption_key,
                ) as device:
                    fw = await device.read_firmware_version()
                    is_flex = device.is_flex
                    # Capture while connected: landing_url() reads the advertised name.
                    landing_url = device.landing_url()
            device_config = device.config
            if TYPE_CHECKING:
                assert device_config is not None
        except (AuthenticationFailedError, AuthenticationRequiredError) as err:
            _LOGGER.warning(
                "%s: device rejected the encryption key during setup (%s); "
                "reauthentication required",
                address,
                err,
            )
            raise ConfigEntryAuthFailed(
                f"Encryption key rejected by OpenDisplay device: {err}"
            ) from err
        except (
            BLEConnectionError,
            BLETimeoutError,
            OpenDisplayError,
            TimeoutError,
        ) as err:
            cached = _cache_setup_if_sleepy(entry)
            if cached is None:
                raise ConfigEntryNotReady(
                    f"Failed to connect to OpenDisplay device: {err}"
                ) from err
            fw = cached.firmware
            is_flex = cached.is_flex
            device_config = cached.device_config
            landing_url = cached.landing_url
            from_cache = True

    # Imported here rather than at module scope because update.py imports from
    # this module — a top-level import would be circular. Sharing the formatter
    # keeps the device registry and the update entity from disagreeing about the
    # version, which is what makes an update appear permanently pending.
    from .update import _format_firmware_version

    profile = SleepProfile.from_entry(entry, device_config)
    coordinator = OpenDisplayCoordinator(hass, address, device_config.binary_inputs)

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
        sw_version=_format_firmware_version(fw["major"], fw["minor"], fw.get("patch")),
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
        sleep_profile=profile,
        ble_lock=async_get_ble_lock(address),
        config_resync_pending=from_cache,
    )

    # Persist a fresh interrogation so the next dark startup can set up from
    # cache; skip when we just loaded from cache (nothing new to store).
    if not from_cache:
        _write_cache(hass, entry, device_config, fw, is_flex, landing_url)

    # Keep entities available across sleep cycles: without this, any sleep
    # interval longer than the ~5 min staleness horizon flaps everything
    # unavailable once per cycle.
    if profile.is_sleepy:
        async_set_fallback_availability_interval(
            hass, address, profile.availability_interval
        )

    manager = DeliveryManager(hass, entry)
    entry.runtime_data.delivery = manager

    await hass.config_entries.async_forward_entry_setups(
        entry, _get_platforms(entry.runtime_data)
    )
    entry.async_on_unload(coordinator.async_start())
    manager.async_start()

    if from_cache:
        # Re-read firmware/config on the next wake and refresh the cache.
        manager.request_config_resync()

    @callback
    def _schedule_reboot_reload() -> None:
        """React to the device's advertised reboot edge.

        For sleepy devices the firmware's unpersisted reboot flag makes every
        wake look like a reboot, and a reconnecting reload races the wake
        window; route to an opportunistic config resync instead. Non-sleepy
        devices keep the reload behavior.
        """
        if profile.is_sleepy:
            manager.request_config_resync()
        else:
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
    info and platforms. Defers until any in-flight BLE operation on this tag
    (upload, drawcustom, LED, buzzer, OTA) finishes so an unrelated reboot
    detection does not tear the connection out from under it.
    """
    runtime = entry.runtime_data
    if runtime is not None:
        # Wait for any in-flight BLE op to release the link, then reload. We only
        # drain (acquire/release) rather than hold the lock across the reload:
        # async_reload unloads the entry, and async_unload_entry acquires the
        # same lock, so holding it here would deadlock the reload.
        async with runtime.ble_lock:
            pass
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: OpenDisplayConfigEntry
) -> bool:
    """Unload a config entry."""
    runtime = entry.runtime_data
    # Cancel pending deadline timers and any in-flight delivery task, then abort
    # an in-flight image upload quickly so unload is not blocked on a long BLE
    # transfer, then wait for any other in-flight BLE op (LED, buzzer, OTA,
    # drawcustom) to release the link before tearing the entry down.
    if runtime.delivery is not None:
        await runtime.delivery.async_shutdown()
    if (task := runtime.upload_task) and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    async with runtime.ble_lock:
        pass

    return await hass.config_entries.async_unload_platforms(
        entry, _get_platforms(runtime)
    )
