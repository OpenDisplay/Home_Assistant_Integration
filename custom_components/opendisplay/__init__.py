"""Integration for OpenDisplay BLE e-paper displays."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
import logging
from typing import TYPE_CHECKING, Any

from opendisplay import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    GlobalConfig,
    OpenDisplayDevice,
    OpenDisplayError,
)
try:
    from opendisplay.models.config_json import config_from_json, config_to_json
except ImportError:
    config_from_json = None
    config_to_json = None

from homeassistant.components.bluetooth import (
    async_ble_device_from_address,
    async_clear_advertisement_history,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from opendisplay.models import FirmwareVersion
    from .services import PendingDisplayUpload

from .const import (
    CONF_CACHED_DEVICE_CONFIG,
    CONF_CACHED_FIRMWARE,
    CONF_CACHED_IS_FLEX,
    CONF_CACHED_LAST_SEEN,
    CONF_ENCRYPTION_KEY,
    DOMAIN,
)
from .coordinator import OpenDisplayCoordinator
from .deep_sleep import (
    deep_sleep_enabled,
    deep_sleep_seconds,
    deep_sleep_timeout_margin_minutes,
    supports_deep_sleep,
)
from .services import async_register_pending_upload_listener, async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)

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
_CONNECT_SETUP_TIMEOUT_SECONDS = 20
_LAST_SEEN_CACHE_MIN_DELTA_SECONDS = 60


@dataclass
class OpenDisplayRuntimeData:
    """Runtime data for an OpenDisplay config entry."""

    coordinator: OpenDisplayCoordinator
    firmware: FirmwareVersion
    device_config: GlobalConfig
    is_flex: bool
    upload_task: asyncio.Task | None = None
    config_sync_task: asyncio.Task | None = None
    pending_upload: PendingDisplayUpload | None = None
    pending_upload_task: asyncio.Task | None = None
    pending_upload_expiry_unsub: Callable[[], None] | None = None


type OpenDisplayConfigEntry = ConfigEntry[OpenDisplayRuntimeData]


def _serialize_device_config(device_config: GlobalConfig) -> dict[str, Any] | None:
    """Serialize GlobalConfig into plain dict for ConfigEntry storage."""
    if config_to_json is not None:
        try:
            dumped = config_to_json(device_config)
        except Exception:
            pass
        else:
            if isinstance(dumped, dict):
                return dumped
    if hasattr(device_config, "model_dump"):
        dumped = device_config.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if hasattr(device_config, "dict"):
        dumped = device_config.dict()
        if isinstance(dumped, dict):
            return dumped
    if hasattr(device_config, "to_dict"):
        dumped = device_config.to_dict()
        if isinstance(dumped, dict):
            return dumped
    if is_dataclass(device_config):
        dumped = asdict(device_config)
        if isinstance(dumped, dict):
            return dumped
    return None


def _deserialize_device_config(raw: object) -> GlobalConfig | None:
    """Deserialize plain dict into GlobalConfig."""
    if not isinstance(raw, dict):
        return None
    if config_from_json is not None:
        try:
            return config_from_json(raw)
        except Exception:
            pass
    if hasattr(GlobalConfig, "model_validate"):
        try:
            return GlobalConfig.model_validate(raw)
        except Exception:
            pass
    if hasattr(GlobalConfig, "parse_obj"):
        try:
            return GlobalConfig.parse_obj(raw)
        except Exception:
            pass
    if hasattr(GlobalConfig, "from_dict"):
        try:
            return GlobalConfig.from_dict(raw)
        except Exception:
            pass
    try:
        return GlobalConfig(**raw)
    except Exception:
        return None


def _normalize_stored_encryption_key(raw: object) -> str | None:
    """Normalize stored encryption key into a lowercase hex string."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.strip().lower()
    if isinstance(raw, (bytes, bytearray)):
        raw_bytes = bytes(raw)
        if len(raw_bytes) == 16:
            return raw_bytes.hex()
        try:
            return raw_bytes.decode().strip().lower()
        except UnicodeDecodeError:
            return None
    return None


def _contains_bytes(value: object) -> bool:
    """Return True if the structure contains raw bytes."""
    if isinstance(value, (bytes, bytearray)):
        return True
    if isinstance(value, Mapping):
        return any(_contains_bytes(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_bytes(item) for item in value)
    return False


def _normalize_entry_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize config entry data so Home Assistant can persist it."""
    normalized = dict(data)

    raw_key = normalized.get(CONF_ENCRYPTION_KEY)
    normalized_key = _normalize_stored_encryption_key(raw_key)
    if raw_key is None:
        normalized.pop(CONF_ENCRYPTION_KEY, None)
    elif normalized_key is None:
        normalized.pop(CONF_ENCRYPTION_KEY, None)
    else:
        normalized[CONF_ENCRYPTION_KEY] = normalized_key

    raw_device_config = normalized.get(CONF_CACHED_DEVICE_CONFIG)
    if isinstance(raw_device_config, dict):
        if _deserialize_device_config(raw_device_config) is None or _contains_bytes(
            raw_device_config
        ):
            normalized.pop(CONF_CACHED_DEVICE_CONFIG, None)
            normalized.pop(CONF_CACHED_FIRMWARE, None)
            normalized.pop(CONF_CACHED_IS_FLEX, None)
            normalized.pop(CONF_CACHED_LAST_SEEN, None)
    elif raw_device_config is not None:
        normalized.pop(CONF_CACHED_DEVICE_CONFIG, None)
        normalized.pop(CONF_CACHED_FIRMWARE, None)
        normalized.pop(CONF_CACHED_IS_FLEX, None)
        normalized.pop(CONF_CACHED_LAST_SEEN, None)

    raw_last_seen = normalized.get(CONF_CACHED_LAST_SEEN)
    if raw_last_seen is not None:
        last_seen = _cached_last_seen(normalized)
        if last_seen is None:
            normalized.pop(CONF_CACHED_LAST_SEEN, None)
        else:
            normalized[CONF_CACHED_LAST_SEEN] = last_seen

    return normalized


def _cached_last_seen(entry_data: Mapping[str, Any]) -> float | None:
    """Return cached last seen timestamp if present and valid."""
    raw_last_seen = entry_data.get(CONF_CACHED_LAST_SEEN)
    if raw_last_seen is None:
        return None
    try:
        last_seen = float(raw_last_seen)
    except (TypeError, ValueError):
        return None
    if last_seen <= 0:
        return None
    return last_seen


def _cached_runtime_data(
    entry_data: Mapping[str, Any],
) -> tuple[FirmwareVersion, GlobalConfig, bool] | None:
    """Return cached runtime metadata if valid."""
    raw_firmware = entry_data.get(CONF_CACHED_FIRMWARE)
    raw_device_config = entry_data.get(CONF_CACHED_DEVICE_CONFIG)
    raw_is_flex = entry_data.get(CONF_CACHED_IS_FLEX)
    if not isinstance(raw_firmware, dict) or not isinstance(raw_is_flex, bool):
        return None
    device_config = _deserialize_device_config(raw_device_config)
    if device_config is None:
        return None
    return raw_firmware, device_config, raw_is_flex


def _deep_sleep_seconds(device_config: GlobalConfig) -> int:
    """Return deep sleep duration from device config."""
    return deep_sleep_seconds(device_config)


def _log_config_changes(
    address: str,
    previous_config: GlobalConfig,
    latest_config: GlobalConfig,
) -> None:
    """Log config changes detected between cached and live device config."""
    previous = _serialize_device_config(previous_config)
    latest = _serialize_device_config(latest_config)
    if (
        not isinstance(previous, dict)
        or not isinstance(latest, dict)
        or previous == latest
    ):
        return

    changed_keys = sorted(
        key
        for key in (set(previous.keys()) | set(latest.keys()))
        if previous.get(key) != latest.get(key)
    )
    _LOGGER.info(
        "%s: Device config changed; syncing Home Assistant cache (changed keys: %s)",
        address,
        ", ".join(changed_keys) if changed_keys else "unknown",
    )


def _cache_runtime_data(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    firmware: FirmwareVersion,
    device_config: GlobalConfig,
    is_flex: bool,
    last_seen: float | None = None,
) -> None:
    """Persist runtime metadata so sleeping devices can restore quickly."""
    if not isinstance(firmware, dict):
        return
    serialized = _serialize_device_config(device_config)
    if serialized is None:
        return
    data = dict(entry.data)
    data[CONF_CACHED_FIRMWARE] = firmware
    data[CONF_CACHED_DEVICE_CONFIG] = serialized
    data[CONF_CACHED_IS_FLEX] = is_flex
    if last_seen is not None:
        data[CONF_CACHED_LAST_SEEN] = last_seen
    hass.config_entries.async_update_entry(entry, data=data)


def _cache_last_seen(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    last_seen: float | None,
) -> None:
    """Persist last seen without writing storage for every BLE advertisement."""
    if last_seen is None or last_seen <= 0:
        return
    previous_last_seen = _cached_last_seen(entry.data)
    if (
        previous_last_seen is not None
        and last_seen - previous_last_seen < _LAST_SEEN_CACHE_MIN_DELTA_SECONDS
    ):
        return
    data = dict(entry.data)
    data[CONF_CACHED_LAST_SEEN] = last_seen
    hass.config_entries.async_update_entry(entry, data=data)
    _LOGGER.debug(
        "%s: Cached last_seen persisted (last_seen=%s, previous_last_seen=%s)",
        getattr(entry, "unique_id", "unknown"),
        dt_util.utc_from_timestamp(last_seen).isoformat(),
        dt_util.utc_from_timestamp(previous_last_seen).isoformat()
        if previous_last_seen is not None
        else None,
    )


def _get_encryption_key(entry_data: Mapping[str, Any]) -> bytes | None:
    """Return the encryption key bytes from entry data, or None."""
    raw = _normalize_stored_encryption_key(entry_data.get(CONF_ENCRYPTION_KEY))
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
    entry_data = _normalize_entry_data(entry.data)
    if entry_data != entry.data:
        hass.config_entries.async_update_entry(entry, data=entry_data)

    address = entry.unique_id
    if TYPE_CHECKING:
        assert address is not None

    cached_runtime = _cached_runtime_data(entry_data)
    cached_last_seen = _cached_last_seen(entry_data)
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    encryption_key = _get_encryption_key(entry_data)
    fw: FirmwareVersion
    device_config: GlobalConfig
    is_flex: bool
    startup_from_cache = False

    if ble_device is None:
        if cached_runtime is None or _deep_sleep_seconds(cached_runtime[1]) <= 0:
            raise ConfigEntryNotReady(
                f"Could not find OpenDisplay device with address {address}"
            )
        fw, device_config, is_flex = cached_runtime
        startup_from_cache = True
        _LOGGER.info(
            "%s: Device not connectable at startup; using cached config "
            "(deep sleep=%ss, startup cache fallback)",
            address,
            _deep_sleep_seconds(device_config),
        )
    else:
        try:
            async with asyncio.timeout(_CONNECT_SETUP_TIMEOUT_SECONDS):
                async with OpenDisplayDevice(
                    mac_address=address,
                    ble_device=ble_device,
                    encryption_key=encryption_key,
                ) as device:
                    fw = await device.read_firmware_version()
                    is_flex = device.is_flex
                    device_config = device.config
                    if TYPE_CHECKING:
                        assert device_config is not None
        except (AuthenticationFailedError, AuthenticationRequiredError) as err:
            raise ConfigEntryAuthFailed(
                f"Encryption key rejected by OpenDisplay device: {err}"
            ) from err
        except TimeoutError as err:
            if cached_runtime is None or _deep_sleep_seconds(cached_runtime[1]) <= 0:
                raise ConfigEntryNotReady(
                    "Timed out while connecting to OpenDisplay device"
                ) from err
            fw, device_config, is_flex = cached_runtime
            startup_from_cache = True
            _LOGGER.info(
                "%s: Startup connection timed out; using cached config "
                "(deep sleep=%ss, startup cache fallback)",
                address,
                _deep_sleep_seconds(device_config),
            )
        except (BLEConnectionError, BLETimeoutError, OpenDisplayError) as err:
            if cached_runtime is None or _deep_sleep_seconds(cached_runtime[1]) <= 0:
                raise ConfigEntryNotReady(
                    f"Failed to connect to OpenDisplay device: {err}"
                ) from err
            fw, device_config, is_flex = cached_runtime
            startup_from_cache = True
            _LOGGER.info(
                "%s: Startup connection failed (%s); using cached config "
                "(deep sleep=%ss, startup cache fallback)",
                address,
                err,
                _deep_sleep_seconds(device_config),
            )
        else:
            _cache_runtime_data(hass, entry, fw, device_config, is_flex)
        finally:
            async_clear_advertisement_history(hass, address)

    coordinator = OpenDisplayCoordinator(
        hass,
        address,
        deep_sleep_time_seconds=_deep_sleep_seconds(device_config),
        deep_sleep_timeout_margin_minutes=deep_sleep_timeout_margin_minutes(
            entry.options
        ),
    )
    if startup_from_cache:
        coordinator.async_startup_from_cache()
    coordinator.async_restore_last_seen(cached_last_seen)

    expected_wakeup = coordinator.expected_wakeup_timestamp
    cached_last_seen_iso = (
        dt_util.utc_from_timestamp(cached_last_seen).isoformat()
        if cached_last_seen is not None
        else None
    )
    _LOGGER.info(
        "%s: Startup diagnostics "
        "(deep_sleep_supported=%s, deep_sleep_enabled=%s, "
        "deep_sleep_seconds=%ss, deep_sleep_timeout_margin=%smin, "
        "availability_window=%ss, ble_connectable_at_startup=%s, "
        "online_at_startup=%s, loaded_from_cache=%s, "
        "coordinator_available=%s, cached_last_seen=%s, expected_wakeup=%s)",
        address,
        supports_deep_sleep(device_config),
        deep_sleep_enabled(device_config),
        _deep_sleep_seconds(device_config),
        coordinator.deep_sleep_timeout_margin_minutes,
        coordinator.deep_sleep_availability_window_seconds,
        ble_device is not None,
        ble_device is not None and not startup_from_cache,
        startup_from_cache,
        coordinator.available,
        cached_last_seen_iso,
        expected_wakeup.isoformat() if expected_wakeup else None,
    )

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
    was_available = coordinator.available

    async def _async_sync_runtime_config() -> None:
        """Refresh firmware/config after the device comes back online."""
        ble_online = async_ble_device_from_address(hass, address, connectable=True)
        if ble_online is None:
            return

        try:
            async with asyncio.timeout(_CONNECT_SETUP_TIMEOUT_SECONDS):
                async with OpenDisplayDevice(
                    mac_address=address,
                    ble_device=ble_online,
                    encryption_key=encryption_key,
                ) as device:
                    latest_fw = await device.read_firmware_version()
                    latest_config = device.config
                    if TYPE_CHECKING:
                        assert latest_config is not None
                    latest_is_flex = device.is_flex
        except TimeoutError:
            _LOGGER.debug("%s: Runtime config sync timed out", address)
            return
        except (AuthenticationFailedError, AuthenticationRequiredError) as err:
            _LOGGER.debug(
                "%s: Skipping runtime config sync due to auth error: %s",
                address,
                err,
            )
            return
        except (BLEConnectionError, BLETimeoutError, OpenDisplayError) as err:
            _LOGGER.debug("%s: Runtime config sync skipped: %s", address, err)
            return
        finally:
            async_clear_advertisement_history(hass, address)

        _log_config_changes(address, entry.runtime_data.device_config, latest_config)
        entry.runtime_data.firmware = latest_fw
        entry.runtime_data.device_config = latest_config
        entry.runtime_data.is_flex = latest_is_flex
        coordinator.async_set_deep_sleep_time_seconds(
            _deep_sleep_seconds(latest_config)
        )
        _cache_runtime_data(hass, entry, latest_fw, latest_config, latest_is_flex)

    # Register coordinator listener to refresh runtime config when the device wakes.
    def _on_coordinator_update() -> None:
        """Handle wake-up transitions and runtime config synchronization."""
        nonlocal was_available
        if coordinator.deep_sleep_time_seconds > 0 and coordinator.data is not None:
            _cache_last_seen(hass, entry, coordinator.data.last_seen)
        available_now = coordinator.available
        if available_now and not was_available:
            current = entry.runtime_data.config_sync_task
            if current is None or current.done():
                entry.runtime_data.config_sync_task = hass.async_create_task(
                    _async_sync_runtime_config(),
                    name=f"opendisplay_sync_config_{address}",
                )
        was_available = available_now

    entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))
    entry.async_on_unload(async_register_pending_upload_listener(hass, entry))

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
    if (task := entry.runtime_data.upload_task) and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if (task := entry.runtime_data.pending_upload_task) and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    entry.runtime_data.pending_upload_task = None
    if (unsub := entry.runtime_data.pending_upload_expiry_unsub) is not None:
        unsub()
        entry.runtime_data.pending_upload_expiry_unsub = None
    if (task := entry.runtime_data.config_sync_task) and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return await hass.config_entries.async_unload_platforms(
        entry, _get_platforms(entry.runtime_data)
    )
