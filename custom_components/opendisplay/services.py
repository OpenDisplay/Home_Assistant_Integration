"""Service registration for the OpenDisplay integration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum
import io
import logging
import os
from typing import TYPE_CHECKING, Any

import aiohttp
from epaper_dithering import ColorScheme
from odl_renderer import generate_image
from opendisplay import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    DitherMode,
    FitMode,
    LedFlashConfig,
    LedFlashStep,
    BuzzerActivateConfig,
    OpenDisplayDevice,
    OpenDisplayError,
    RefreshMode,
    Rotation,
)
from PIL import Image as PILImage, ImageOps
import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothReachabilityIntent,
    async_address_reachability_diagnostics,
    async_ble_device_from_address,
    async_clear_advertisement_history,
)
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_source import async_resolve_media
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.network import get_url
from homeassistant.helpers.selector import MediaSelector, MediaSelectorConfig
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from . import OpenDisplayConfigEntry

from .const import (
    CONF_ENCRYPTION_KEY,
    DOMAIN,
    SIGNAL_DEVICE_SEEN,
    SIGNAL_IMAGE_UPDATED,
    SIGNAL_PENDING_UPLOAD,
)
from .deep_sleep import (
    availability_window_seconds,
    deep_sleep_enabled,
    deep_sleep_seconds,
    deep_sleep_timeout_margin_minutes,
    supports_deep_sleep,
)

ATTR_IMAGE = "image"
ATTR_ROTATION = "rotation"
ATTR_DITHER_MODE = "dither_mode"
ATTR_REFRESH_MODE = "refresh_mode"
ATTR_FIT_MODE = "fit_mode"
ATTR_TONE_COMPRESSION = "tone_compression"
_PENDING_UPLOAD_WAKE_SETTLE_DELAY_SECONDS = 3
_UPLOAD_RETRY_DELAY_SECONDS = 5
_UPLOAD_MAX_ATTEMPTS = 3


@dataclass(slots=True)
class PendingDisplayUpload:
    """Stored image payload waiting for the next wake-up window."""

    image: PILImage.Image
    dither_mode: DitherMode
    refresh_mode: RefreshMode
    fit: FitMode = FitMode.CONTAIN
    tone: float | str = "auto"
    rotate: Rotation = Rotation.ROTATE_0
    source: str = "unknown"
    created_at: datetime = field(default_factory=dt_util.utcnow)
    expires_at: datetime | None = None


def _str_to_int_enum(enum_class: type[IntEnum]) -> Callable[[str], Any]:
    """Convert a lowercase enum name string to an enum member."""
    members = {m.name.lower(): m for m in enum_class}

    def validate(value: str) -> IntEnum:
        if (result := members.get(value)) is None:
            raise vol.Invalid(f"Invalid value: {value}")
        return result

    return validate


def _coerce_none_to_default(default: Any) -> Callable[[Any], Any]:
    """Map Home Assistant's 'none' sentinel to a schema default value."""

    def validate(value: Any) -> Any:
        if isinstance(value, str) and value.lower() == "none":
            return default
        return value

    return validate


def _dither_value(value: Any) -> DitherMode:
    """Accept new dither names ("ordered") and legacy numeric values (0/1/2...)."""
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.lstrip("-").isdigit()
    ):
        try:
            return DitherMode(int(value))
        except ValueError as err:
            raise vol.Invalid(f"Invalid dither value: {value}") from err
    return _str_to_int_enum(DitherMode)(value)


def _refresh_type_value(value: Any) -> RefreshMode:
    """Accept names ("full"/"fast") and legacy numeric values."""
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.lstrip("-").isdigit()
    ):
        n = int(value)
        if n in (2, 3):
            return RefreshMode.FAST
        try:
            mode = RefreshMode(n)
        except ValueError as err:
            raise vol.Invalid(f"Invalid refresh_type: {value}") from err
    else:
        mode = _str_to_int_enum(RefreshMode)(value)
    if mode is RefreshMode.PARTIAL:
        return RefreshMode.FAST
    return mode


SCHEMA_UPLOAD_IMAGE = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_IMAGE): vol.Any(
            cv.url, MediaSelector(MediaSelectorConfig(accept=["image/*"]))
        ),
        vol.Optional(ATTR_ROTATION, default=Rotation.ROTATE_0): vol.All(
            _coerce_none_to_default(Rotation.ROTATE_0),
            vol.Coerce(int),
            vol.Coerce(Rotation),
        ),
        vol.Optional(ATTR_DITHER_MODE, default="burkes"): _str_to_int_enum(DitherMode),
        vol.Optional(ATTR_REFRESH_MODE, default="full"): _str_to_int_enum(RefreshMode),
        vol.Optional(ATTR_FIT_MODE, default="contain"): _str_to_int_enum(FitMode),
        vol.Optional(ATTR_TONE_COMPRESSION): vol.All(
            vol.Coerce(float), vol.Range(min=0.0, max=100.0)
        ),
    }
)


SCHEMA_DRAWCUSTOM = vol.Schema(
    {
        vol.Optional("device_id", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("label_id", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("area_id", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Required("payload"): list,
        vol.Optional("background", default="white"): cv.string,
        vol.Optional("rotate", default=0): vol.All(
            _coerce_none_to_default(0), vol.Coerce(int), vol.In([0, 90, 180, 270])
        ),
        vol.Optional("dither", default="ordered"): vol.All(
            _coerce_none_to_default("ordered"), _dither_value
        ),
        vol.Optional("refresh_type", default="full"): vol.All(
            _coerce_none_to_default("full"), _refresh_type_value
        ),
        vol.Optional(ATTR_TONE_COMPRESSION): vol.All(
            vol.Coerce(float), vol.Range(min=0.0, max=100.0)
        ),
        vol.Optional("dry-run", default=False): cv.boolean,
    },
    # Silently drop legacy keys (ttl, preload_type, preload_lut, ...).
    extra=vol.REMOVE_EXTRA,
)


def _rgb_to_led_color(value: list[int]) -> int:
    """Convert [R, G, B] (0-255 each) to packed 8-bit LED color byte (3R 3G 2B)."""
    r, g, b = value
    return (
        ((round(r * 7 / 255)) << 5)
        | ((round(g * 7 / 255)) << 2)
        | (round(b * 3 / 255))
    )


def _ms_to_loop_delay(value: int) -> int:
    """Convert milliseconds to 4-bit loop delay units (×100 ms each, 0–1500 ms)."""
    return max(0, min(15, round(value / 100)))


def _ms_to_inter_delay(value: int) -> int:
    """Convert milliseconds to 8-bit inter-delay units (×100 ms each, 0–25500 ms)."""
    return max(0, min(255, round(value / 100)))


def _led_step_fields(
    n: int,
    *,
    color_default: list[int],
    flash_count_default: int,
) -> dict:
    """Return the voluptuous field definitions for one LED step."""
    return {
        vol.Optional(f"color{n}", default=color_default): _rgb_to_led_color,
        vol.Optional(f"flash_count{n}", default=flash_count_default): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=15)
        ),
        vol.Optional(f"loop_delay{n}", default=0): vol.All(
            vol.Coerce(int), _ms_to_loop_delay
        ),
        vol.Optional(f"inter_delay{n}", default=0): vol.All(
            vol.Coerce(int), _ms_to_inter_delay
        ),
    }


SCHEMA_ACTIVATE_LED = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional("instance", default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=255)
        ),
        vol.Optional("brightness", default=8): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=16)
        ),
        vol.Optional("repeats", default=1): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=255)
        ),
        **_led_step_fields(1, color_default=[255, 0, 0], flash_count_default=1),
        **_led_step_fields(2, color_default=[0, 255, 0], flash_count_default=0),
        **_led_step_fields(3, color_default=[0, 0, 255], flash_count_default=0),
    }
)

SCHEMA_ACTIVATE_BUZZER = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional("instance", default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=3)
        ),
        vol.Optional("frequency_hz", default=1000): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=12000)
        ),
        vol.Optional("duration_ms", default=100): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=1275)
        ),
        vol.Optional("repeats", default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=255)
        ),
    }
)


def _get_entry_for_device(call: ServiceCall) -> OpenDisplayConfigEntry:
    """Return the config entry for the device targeted by a service call."""
    device_id: str = call.data[ATTR_DEVICE_ID]
    device_registry = dr.async_get(call.hass)

    if (device := device_registry.async_get(device_id)) is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_device_id",
            translation_placeholders={"device_id": device_id},
        )

    mac_address = next(
        (conn[1] for conn in device.connections if conn[0] == CONNECTION_BLUETOOTH),
        None,
    )
    if mac_address is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_device_id",
            translation_placeholders={"device_id": device_id},
        )

    entry = call.hass.config_entries.async_entry_for_domain_unique_id(
        DOMAIN, mac_address
    )
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"address": mac_address},
        )

    return entry


def _pil_to_jpeg(img: PILImage.Image) -> bytes:
    """Encode a PIL image as JPEG bytes."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _pending_age_seconds(pending: PendingDisplayUpload) -> int:
    """Return pending upload age in seconds."""
    return max(0, int((dt_util.utcnow() - pending.created_at).total_seconds()))


def _pending_upload_timeout_seconds(entry: "OpenDisplayConfigEntry") -> int:
    """Return how long a pending upload may wait for a sleeping device."""
    coordinator = entry.runtime_data.coordinator
    availability_window = int(
        getattr(coordinator, "deep_sleep_availability_window_seconds", 0) or 0
    )
    if availability_window > 0:
        return availability_window

    sleep_seconds = deep_sleep_seconds(entry.runtime_data.device_config)
    return availability_window_seconds(
        sleep_seconds,
        deep_sleep_timeout_margin_minutes(entry.options),
    )


def _cancel_pending_upload_expiry(entry: "OpenDisplayConfigEntry") -> None:
    """Cancel a pending upload expiry timer if one exists."""
    if (
        unsub := getattr(entry.runtime_data, "pending_upload_expiry_unsub", None)
    ) is None:
        return
    unsub()
    entry.runtime_data.pending_upload_expiry_unsub = None


def _cancel_pending_upload_task(entry: "OpenDisplayConfigEntry") -> None:
    """Cancel an in-flight pending upload task if one exists."""
    task = getattr(entry.runtime_data, "pending_upload_task", None)
    if task is None or task.done():
        return
    task.cancel()
    entry.runtime_data.pending_upload_task = None


def _clear_pending_upload(
    hass: HomeAssistant,
    entry: "OpenDisplayConfigEntry",
    *,
    reason: str,
    cancel_task: bool = False,
) -> bool:
    """Clear the current queued image upload."""
    address = entry.unique_id
    assert address is not None
    pending = entry.runtime_data.pending_upload
    if pending is None:
        _cancel_pending_upload_expiry(entry)
        if cancel_task:
            _cancel_pending_upload_task(entry)
        return False
    if cancel_task:
        _cancel_pending_upload_task(entry)
    _cancel_pending_upload_expiry(entry)
    entry.runtime_data.pending_upload = None
    entry.runtime_data.coordinator.async_set_pending_upload(False)
    async_dispatcher_send(hass, f"{SIGNAL_PENDING_UPLOAD}_{address}")
    _LOGGER.info(
        "%s: Pending upload cleared "
        "(reason=%s, source=%s, age=%ss, expires_at=%s)",
        address,
        reason,
        pending.source,
        _pending_age_seconds(pending),
        pending.expires_at.isoformat() if pending.expires_at else None,
    )
    return True


def _replace_pending_upload(
    hass: HomeAssistant,
    entry: "OpenDisplayConfigEntry",
    pending: PendingDisplayUpload,
) -> int:
    """Store one pending image upload for this device, replacing any older one."""
    address = entry.unique_id
    assert address is not None
    previous = entry.runtime_data.pending_upload
    if previous is not None:
        _LOGGER.info(
            "%s: Replacing pending upload "
            "(old_source=%s, old_age=%ss, old_expires_at=%s, "
            "new_source=%s)",
            address,
            previous.source,
            _pending_age_seconds(previous),
            previous.expires_at.isoformat() if previous.expires_at else None,
            pending.source,
        )
    _cancel_pending_upload_task(entry)
    entry.runtime_data.pending_upload = pending
    timeout_seconds = _schedule_pending_upload_expiry(hass, entry, pending)
    entry.runtime_data.coordinator.async_set_pending_upload(True)
    async_dispatcher_send(hass, f"{SIGNAL_PENDING_UPLOAD}_{address}")
    return timeout_seconds


def _drop_pending_upload(
    hass: HomeAssistant,
    entry: "OpenDisplayConfigEntry",
    pending: PendingDisplayUpload,
    *,
    reason: str,
) -> None:
    """Drop a queued image and update diagnostics."""
    address = entry.unique_id
    assert address is not None
    if entry.runtime_data.pending_upload is not pending:
        return
    _cancel_pending_upload_expiry(entry)
    entry.runtime_data.pending_upload = None
    entry.runtime_data.coordinator.async_set_pending_upload(False)
    async_dispatcher_send(hass, f"{SIGNAL_PENDING_UPLOAD}_{address}")
    coordinator = entry.runtime_data.coordinator
    expected_wakeup = getattr(coordinator, "expected_wakeup_timestamp", None)
    availability_deadline = getattr(
        coordinator,
        "deep_sleep_availability_deadline_timestamp",
        None,
    )
    _LOGGER.error(
        "%s: Pending upload dropped "
        "(reason=%s, source=%s, age=%ss, created_at=%s, expires_at=%s, "
        "deep_sleep=%ss, coordinator_available=%s, expected_wakeup=%s, "
        "availability_deadline=%s)",
        address,
        reason,
        pending.source,
        _pending_age_seconds(pending),
        pending.created_at.isoformat(),
        pending.expires_at.isoformat() if pending.expires_at else None,
        deep_sleep_seconds(entry.runtime_data.device_config),
        coordinator.available,
        expected_wakeup.isoformat() if expected_wakeup else None,
        availability_deadline.isoformat() if availability_deadline else None,
    )


def _schedule_pending_upload_expiry(
    hass: HomeAssistant,
    entry: "OpenDisplayConfigEntry",
    pending: PendingDisplayUpload,
) -> int:
    """Schedule expiration for a queued image upload."""
    address = entry.unique_id
    assert address is not None
    _cancel_pending_upload_expiry(entry)
    timeout_seconds = _pending_upload_timeout_seconds(entry)
    pending.expires_at = dt_util.utcnow() + timedelta(seconds=timeout_seconds)

    @callback
    def _expire_pending_upload(_now: datetime) -> None:
        _drop_pending_upload(
            hass,
            entry,
            pending,
            reason="device did not return before pending upload timeout",
        )

    entry.runtime_data.pending_upload_expiry_unsub = async_call_later(
        hass,
        timeout_seconds,
        _expire_pending_upload,
    )
    _LOGGER.debug(
        "%s: Pending upload expiry scheduled "
        "(timeout=%ss, expires_at=%s, source=%s)",
        address,
        timeout_seconds,
        pending.expires_at.isoformat(),
        pending.source,
    )
    return timeout_seconds


def _load_image(path: str) -> PILImage.Image:
    """Load an image from disk and apply EXIF orientation."""
    image = PILImage.open(path)
    image.load()
    return ImageOps.exif_transpose(image)


def _load_image_from_bytes(data: bytes) -> PILImage.Image:
    """Load an image from bytes and apply EXIF orientation."""
    image = PILImage.open(io.BytesIO(data))
    image.load()
    return ImageOps.exif_transpose(image)


async def _async_download_image(hass: HomeAssistant, url: str) -> PILImage.Image:
    """Download an image from a URL and return a PIL Image."""
    if not url.startswith(("http://", "https://")):
        url = get_url(hass) + async_sign_path(
            hass, url, timedelta(minutes=5), use_content_user=True
        )
    session = async_get_clientsession(hass)
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
    except aiohttp.ClientError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="media_download_error",
            translation_placeholders={"error": str(err)},
        ) from err

    return await hass.async_add_executor_job(_load_image_from_bytes, data)


async def _async_connect_and_run(
    hass: HomeAssistant,
    entry: "OpenDisplayConfigEntry",
    action: Callable[[Any], Awaitable[None]],
    *,
    wrap_connection_errors: bool = True,
) -> None:
    """Resolve BLE device, open a connection, run action, handle auth errors."""
    address = entry.unique_id
    assert address is not None
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        _LOGGER.debug(
            "%s: BLE device not connectable for OpenDisplay action "
            "(wrap_connection_errors=%s)",
            address,
            wrap_connection_errors,
        )
        if not wrap_connection_errors:
            # Treat a missing BLE cache entry as a retryable connection failure so
            # callers like the deep-sleep flush keep the queued upload instead of
            # dropping it when the connectable cache expires between the pre-check
            # and the actual connect attempt.
            raise BLEConnectionError(f"OpenDisplay device {address} not connectable")
        raise HomeAssistantError(
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

    raw_key = entry.data.get(CONF_ENCRYPTION_KEY)
    if isinstance(raw_key, (bytes, bytearray)):
        raw_key = bytes(raw_key).hex()
    if raw_key is not None and len(raw_key) != 32:
        entry.async_start_reauth(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="authentication_error"
        )
    try:
        encryption_key = bytes.fromhex(raw_key) if raw_key is not None else None
    except ValueError as err:
        entry.async_start_reauth(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="authentication_error"
        ) from err

    try:
        _LOGGER.debug(
            "%s: Opening OpenDisplay BLE connection "
            "(wrap_connection_errors=%s, cached_config=%s, encrypted=%s)",
            address,
            wrap_connection_errors,
            entry.runtime_data.device_config is not None,
            encryption_key is not None,
        )
        async with OpenDisplayDevice(
            mac_address=address,
            ble_device=ble_device,
            config=entry.runtime_data.device_config,
            encryption_key=encryption_key,
        ) as device:
            await action(device)
        _LOGGER.debug("%s: OpenDisplay BLE action finished", address)
    except (AuthenticationFailedError, AuthenticationRequiredError) as err:
        entry.async_start_reauth(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="authentication_error"
        ) from err
    except (BLEConnectionError, BLETimeoutError) as err:
        if not wrap_connection_errors:
            raise
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="upload_error",
            translation_placeholders={"error": str(err)},
        ) from err
    except OpenDisplayError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="upload_error",
            translation_placeholders={"error": str(err)},
        ) from err
    finally:
        async_clear_advertisement_history(hass, address)


async def _async_send_image_now(
    hass: HomeAssistant,
    entry: "OpenDisplayConfigEntry",
    pending: PendingDisplayUpload,
) -> None:
    """Upload image immediately and update image entity cache."""
    address = entry.unique_id
    assert address is not None
    _LOGGER.debug(
        "%s: Sending image now "
        "(source=%s, image_size=%s, refresh_mode=%s, dither_mode=%s, "
        "fit=%s, rotate=%s, tone=%s, created_at=%s)",
        address,
        pending.source,
        getattr(pending.image, "size", None),
        pending.refresh_mode,
        pending.dither_mode,
        pending.fit,
        pending.rotate,
        pending.tone,
        pending.created_at,
    )

    async def _upload(device: OpenDisplayDevice) -> None:
        await device.upload_image(
            pending.image,
            refresh_mode=pending.refresh_mode,
            dither_mode=pending.dither_mode,
            tone=pending.tone,
            fit=pending.fit,
            rotate=pending.rotate,
        )

    await _async_connect_and_run(hass, entry, _upload)

    jpeg = await hass.async_add_executor_job(_pil_to_jpeg, pending.image)
    async_dispatcher_send(hass, f"{SIGNAL_IMAGE_UPDATED}_{entry.unique_id}", jpeg)
    _LOGGER.info(
        "%s: Image uploaded successfully (source=%s, image_size=%s)",
        address,
        pending.source,
        getattr(pending.image, "size", None),
    )


async def _async_send_image_with_retries(
    hass: HomeAssistant,
    entry: "OpenDisplayConfigEntry",
    pending: PendingDisplayUpload,
    *,
    context: str,
    settle_delay_seconds: int = 0,
    should_continue: Callable[[], bool] | None = None,
) -> bool:
    """Upload an image with consistent retry behavior."""
    address = entry.unique_id
    assert address is not None
    if settle_delay_seconds > 0:
        _LOGGER.info(
            "%s: Waiting before upload attempts "
            "(context=%s, source=%s, settle_delay=%ss)",
            address,
            context,
            pending.source,
            settle_delay_seconds,
        )
        await asyncio.sleep(settle_delay_seconds)

    for attempt in range(1, _UPLOAD_MAX_ATTEMPTS + 1):
        if should_continue is not None and not should_continue():
            _LOGGER.info(
                "%s: Upload attempts stopped before attempt %s/%s "
                "(context=%s, source=%s)",
                address,
                attempt,
                _UPLOAD_MAX_ATTEMPTS,
                context,
                pending.source,
            )
            return False

        _LOGGER.info(
            "%s: Upload attempt %s/%s "
            "(context=%s, source=%s, age=%ss, expires_at=%s)",
            address,
            attempt,
            _UPLOAD_MAX_ATTEMPTS,
            context,
            pending.source,
            _pending_age_seconds(pending),
            pending.expires_at.isoformat() if pending.expires_at else None,
        )
        try:
            await _async_send_image_now(hass, entry, pending)
        except HomeAssistantError as err:
            if attempt >= _UPLOAD_MAX_ATTEMPTS:
                _LOGGER.info(
                    "%s: Upload failed after %s attempts "
                    "(context=%s, source=%s, age=%ss, expires_at=%s, error=%s)",
                    address,
                    attempt,
                    context,
                    pending.source,
                    _pending_age_seconds(pending),
                    pending.expires_at.isoformat() if pending.expires_at else None,
                    err,
                )
                raise
            _LOGGER.info(
                "%s: Upload attempt %s/%s failed; retrying in %ss "
                "(context=%s, source=%s, error=%s)",
                address,
                attempt,
                _UPLOAD_MAX_ATTEMPTS,
                _UPLOAD_RETRY_DELAY_SECONDS,
                context,
                pending.source,
                err,
            )
            await asyncio.sleep(_UPLOAD_RETRY_DELAY_SECONDS)
            continue
        _LOGGER.info(
            "%s: Upload attempts succeeded "
            "(context=%s, source=%s, attempt=%s/%s)",
            address,
            context,
            pending.source,
            attempt,
            _UPLOAD_MAX_ATTEMPTS,
        )
        return True

    return False


async def _async_queue_or_send_image(
    hass: HomeAssistant,
    entry: "OpenDisplayConfigEntry",
    pending: PendingDisplayUpload,
) -> None:
    """Send immediately when awake, otherwise keep as pending upload."""
    address = entry.unique_id
    assert address is not None

    device_config = entry.runtime_data.device_config
    deep_sleep_supported = supports_deep_sleep(device_config)
    deep_sleep_active = deep_sleep_supported and deep_sleep_enabled(device_config)
    coordinator = entry.runtime_data.coordinator
    coordinator_available = getattr(coordinator, "available", True)
    expected_wakeup = getattr(coordinator, "expected_wakeup_timestamp", None)
    availability_deadline = getattr(
        coordinator,
        "deep_sleep_availability_deadline_timestamp",
        None,
    )
    timeout_margin = getattr(
        coordinator,
        "deep_sleep_timeout_margin_minutes",
        None,
    )
    availability_window = getattr(
        coordinator,
        "deep_sleep_availability_window_seconds",
        None,
    )
    ble_device = async_ble_device_from_address(hass, address, connectable=True)

    _LOGGER.info(
        "%s: Upload decision "
        "(source=%s, image_size=%s, deep_sleep_supported=%s, "
        "deep_sleep_enabled=%s, deep_sleep=%ss, timeout_margin=%smin, "
        "availability_window=%ss, connectable=%s, coordinator_available=%s, "
        "expected_wakeup=%s, availability_deadline=%s, pending_already_queued=%s)",
        address,
        pending.source,
        getattr(pending.image, "size", None),
        deep_sleep_supported,
        deep_sleep_active,
        deep_sleep_seconds(device_config),
        timeout_margin,
        availability_window,
        ble_device is not None,
        coordinator_available,
        expected_wakeup.isoformat() if expected_wakeup else None,
        availability_deadline.isoformat() if availability_deadline else None,
        entry.runtime_data.pending_upload is not None,
    )

    if not deep_sleep_active:
        _clear_pending_upload(
            hass,
            entry,
            reason="new upload request for non-deep-sleep device",
            cancel_task=True,
        )
        try:
            await _async_send_image_with_retries(
                hass,
                entry,
                pending,
                context="initial non-deep-sleep upload",
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "%s: Upload failed after retry attempts for non-deep-sleep "
                "device; not queueing (error=%s)",
                address,
                err,
            )
            raise
        return

    if ble_device is not None and coordinator_available:
        _LOGGER.info(
            "%s: Deep-sleep device appears awake; attempting upload before "
            "queueing (source=%s)",
            address,
            pending.source,
        )
        _clear_pending_upload(
            hass,
            entry,
            reason="new upload request for awake deep-sleep device",
            cancel_task=True,
        )
        try:
            await _async_send_image_with_retries(
                hass,
                entry,
                pending,
                context="initial awake deep-sleep upload",
            )
            return
        except HomeAssistantError as err:
            _LOGGER.info(
                "%s: Upload failed after retry attempts for awake deep-sleep "
                "device; queueing for next wake-up (error=%s)",
                address,
                err,
                exc_info=True,
            )
    else:
        _LOGGER.info(
            "%s: Deep-sleep device appears asleep; queueing without immediate "
            "upload attempts (source=%s, connectable=%s, coordinator_available=%s)",
            address,
            pending.source,
            ble_device is not None,
            coordinator_available,
        )

    _LOGGER.info(
        "%s: Deep-sleep upload queued "
        "(source=%s, connectable=%s, coordinator_available=%s, "
        "expected_wakeup=%s, availability_deadline=%s)",
        address,
        pending.source,
        ble_device is not None,
        coordinator_available,
        expected_wakeup.isoformat() if expected_wakeup else None,
        availability_deadline.isoformat() if availability_deadline else None,
    )
    timeout_seconds = _replace_pending_upload(hass, entry, pending)
    _LOGGER.info(
        "%s: Queued image upload "
        "(source=%s, deep_sleep=%ss, timeout=%ss, expires_at=%s)",
        address,
        pending.source,
        deep_sleep_seconds(device_config),
        timeout_seconds,
        pending.expires_at.isoformat() if pending.expires_at else None,
    )


async def _async_try_pending_upload(
    hass: HomeAssistant,
    entry: "OpenDisplayConfigEntry",
) -> None:
    """Attempt to flush queued image upload on advertisement/wake-up."""
    address = entry.unique_id
    assert address is not None

    pending = entry.runtime_data.pending_upload
    if pending is None:
        _LOGGER.debug("%s: No pending upload to flush", address)
        _cancel_pending_upload_expiry(entry)
        entry.runtime_data.coordinator.async_set_pending_upload(False)
        return

    if pending.expires_at is not None and dt_util.utcnow() >= pending.expires_at:
        _drop_pending_upload(
            hass,
            entry,
            pending,
            reason="pending upload expired before flush attempt",
        )
        return

    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    coordinator = entry.runtime_data.coordinator
    expected_wakeup = getattr(coordinator, "expected_wakeup_timestamp", None)
    availability_deadline = getattr(
        coordinator,
        "deep_sleep_availability_deadline_timestamp",
        None,
    )
    _LOGGER.info(
        "%s: Pending upload flush check "
        "(source=%s, age=%ss, expires_at=%s, connectable=%s, "
        "coordinator_available=%s, expected_wakeup=%s, availability_deadline=%s)",
        address,
        pending.source,
        _pending_age_seconds(pending),
        pending.expires_at.isoformat() if pending.expires_at else None,
        ble_device is not None,
        coordinator.available,
        expected_wakeup.isoformat() if expected_wakeup else None,
        availability_deadline.isoformat() if availability_deadline else None,
    )
    if ble_device is None:
        _LOGGER.debug(
            "%s: Pending upload deferred; device not connectable yet "
            "(source=%s, expires_at=%s)",
            address,
            pending.source,
            pending.expires_at.isoformat() if pending.expires_at else None,
        )
        return

    task = entry.runtime_data.pending_upload_task
    if task is not None and not task.done():
        _LOGGER.debug(
            "%s: Pending upload task already running, skipping new attempt "
            "(source=%s)",
            address,
            pending.source,
        )
        return

    async def _runner() -> None:
        current_task = asyncio.current_task()
        try:
            pending_now = entry.runtime_data.pending_upload
            if pending_now is None:
                _LOGGER.debug(
                    "%s: Pending upload vanished before runner start",
                    entry.unique_id,
                )
                _cancel_pending_upload_expiry(entry)
                entry.runtime_data.coordinator.async_set_pending_upload(False)
                return
            _LOGGER.info(
                "%s: Pending upload flush started "
                "(source=%s, age=%ss, expires_at=%s, settle_delay=%ss, "
                "attempts=%s, retry_delay=%ss)",
                address,
                pending_now.source,
                _pending_age_seconds(pending_now),
                pending_now.expires_at.isoformat() if pending_now.expires_at else None,
                _PENDING_UPLOAD_WAKE_SETTLE_DELAY_SECONDS,
                _UPLOAD_MAX_ATTEMPTS,
                _UPLOAD_RETRY_DELAY_SECONDS,
            )

            def _pending_upload_still_current() -> bool:
                if entry.runtime_data.pending_upload is not pending_now:
                    _LOGGER.info(
                        "%s: Pending upload flush stopped because a newer upload "
                        "replaced it (source=%s)",
                        address,
                        pending_now.source,
                    )
                    return False
                if (
                    pending_now.expires_at is not None
                    and dt_util.utcnow() >= pending_now.expires_at
                ):
                    _drop_pending_upload(
                        hass,
                        entry,
                        pending_now,
                        reason="pending upload expired during retry attempts",
                    )
                    return False
                return True

            try:
                delivered = await _async_send_image_with_retries(
                    hass,
                    entry,
                    pending_now,
                    context="pending upload flush",
                    settle_delay_seconds=_PENDING_UPLOAD_WAKE_SETTLE_DELAY_SECONDS,
                    should_continue=_pending_upload_still_current,
                )
            except HomeAssistantError:
                _drop_pending_upload(
                    hass,
                    entry,
                    pending_now,
                    reason="pending upload failed after retry attempts",
                )
                return

            if not delivered:
                return

            if entry.runtime_data.pending_upload is pending_now:
                _cancel_pending_upload_expiry(entry)
                entry.runtime_data.pending_upload = None
                entry.runtime_data.coordinator.async_set_pending_upload(False)
                async_dispatcher_send(hass, f"{SIGNAL_PENDING_UPLOAD}_{address}")
                _LOGGER.info(
                    "%s: Pending upload delivered successfully "
                    "(source=%s)",
                    address,
                    pending_now.source,
                )
        except asyncio.CancelledError:
            _LOGGER.info(
                "%s: Pending upload task cancelled because a newer upload "
                "superseded it",
                address,
            )
            raise
        finally:
            if entry.runtime_data.pending_upload_task is current_task:
                entry.runtime_data.pending_upload_task = None

    entry.runtime_data.pending_upload_task = hass.async_create_task(
        _runner(),
        name=f"opendisplay_pending_upload_{address}",
    )


def async_register_pending_upload_listener(
    hass: HomeAssistant,
    entry: "OpenDisplayConfigEntry",
) -> Callable[[], None]:
    """Listen for BLE advertisements and attempt pending upload flushes."""
    address = entry.unique_id
    assert address is not None

    @callback
    def _schedule_try_pending_upload() -> None:
        pending = entry.runtime_data.pending_upload
        if pending is None:
            return
        task = entry.runtime_data.pending_upload_task
        if task is not None and not task.done():
            _LOGGER.debug(
                "%s: Device-seen signal received but pending upload task is "
                "already running (source=%s)",
                address,
                pending.source,
            )
            return
        _LOGGER.info(
            "%s: Device-seen signal received; scheduling pending upload attempt "
            "(source=%s, age=%ss, expires_at=%s)",
            address,
            pending.source,
            _pending_age_seconds(pending),
            pending.expires_at.isoformat() if pending.expires_at else None,
        )
        hass.async_create_task(
            _async_try_pending_upload(hass, entry),
            name=f"opendisplay_try_pending_{address}",
        )

    return async_dispatcher_connect(
        hass,
        f"{SIGNAL_DEVICE_SEEN}_{address}",
        _schedule_try_pending_upload,
    )


async def _async_upload_image(call: ServiceCall) -> None:
    """Handle the upload_image service call."""
    entry = _get_entry_for_device(call)

    image_data: dict[str, Any] = call.data[ATTR_IMAGE]
    rotation: Rotation = call.data[ATTR_ROTATION]
    dither_mode: DitherMode = call.data[ATTR_DITHER_MODE]
    refresh_mode: RefreshMode = call.data[ATTR_REFRESH_MODE]
    fit_mode: FitMode = call.data[ATTR_FIT_MODE]
    tone_compression_pct: float | None = call.data.get(ATTR_TONE_COMPRESSION)
    tone_compression: float | str = (
        tone_compression_pct / 100.0 if tone_compression_pct is not None else "auto"
    )

    current = asyncio.current_task()
    if (prev := entry.runtime_data.upload_task) is not None and not prev.done():
        prev.cancel()
        # pylint: disable-next=home-assistant-action-swallowed-exception
        with contextlib.suppress(asyncio.CancelledError):
            await prev
    entry.runtime_data.upload_task = current

    try:
        media = await async_resolve_media(
            call.hass, image_data["media_content_id"], None
        )

        if media.path is not None:
            pil_image = await call.hass.async_add_executor_job(
                _load_image, str(media.path)
            )
        else:
            pil_image = await _async_download_image(call.hass, media.url)

        pending = PendingDisplayUpload(
            image=pil_image,
            dither_mode=dither_mode,
            refresh_mode=refresh_mode,
            fit=fit_mode,
            tone=tone_compression,
            rotate=rotation,
            source="upload_image",
        )
        _LOGGER.info(
            "%s: upload_image service resolved media "
            "(image_size=%s, refresh_mode=%s, dither_mode=%s, fit=%s, "
            "rotate=%s, tone=%s)",
            entry.unique_id,
            getattr(pil_image, "size", None),
            refresh_mode,
            dither_mode,
            fit_mode,
            rotation,
            tone_compression,
        )
        await _async_queue_or_send_image(call.hass, entry, pending)
    except asyncio.CancelledError:
        return
    finally:
        if entry.runtime_data.upload_task is current:
            entry.runtime_data.upload_task = None


_LOGGER = logging.getLogger(__name__)


class HADataProvider:
    """Provides HA recorder history data to odl_renderer plot elements."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get_history(
        self,
        entity_ids: list[str],
        start: Any,
        end: Any,
    ) -> dict[str, list[dict]]:
        from functools import partial

        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import get_significant_states

        raw = await get_instance(self._hass).async_add_executor_job(
            partial(
                get_significant_states,
                self._hass,
                start,
                end,
                entity_ids,
                significant_changes_only=False,
                minimal_response=True,
                no_attributes=False,
            )
        )
        result: dict[str, list[dict]] = {}
        for entity_id, states in raw.items():
            if not states:
                result[entity_id] = []
                continue
            first = states[0]
            result[entity_id] = [
                {"state": first.state, "last_changed": str(first.last_changed)},
                *states[1:],
            ]
        return result


def _get_entry_for_device_id(
    hass: HomeAssistant, device_id: str
) -> "OpenDisplayConfigEntry":
    """Return the config entry for a raw device_id string."""
    device_registry = dr.async_get(hass)
    if (device := device_registry.async_get(device_id)) is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_device_id",
            translation_placeholders={"device_id": device_id},
        )
    mac_address = next(
        (conn[1] for conn in device.connections if conn[0] == CONNECTION_BLUETOOTH),
        None,
    )
    if mac_address is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_device_id",
            translation_placeholders={"device_id": device_id},
        )
    entry = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, mac_address)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"address": mac_address},
        )
    return entry


async def _get_device_ids_from_label(hass: HomeAssistant, label_id: str) -> list[str]:
    device_registry = dr.async_get(hass)
    entry_ids = {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)}
    return [
        d.id
        for d in dr.async_entries_for_label(device_registry, label_id)
        if d.config_entries & entry_ids
    ]


async def _get_device_ids_from_area(hass: HomeAssistant, area_id: str) -> list[str]:
    device_registry = dr.async_get(hass)
    entry_ids = {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)}
    return [
        d.id
        for d in dr.async_entries_for_area(device_registry, area_id)
        if d.config_entries & entry_ids
    ]


async def _async_drawcustom(call: ServiceCall) -> None:
    """Handle the drawcustom service call."""
    hass = call.hass

    device_ids: list[str] = list(call.data["device_id"])
    for label_id in call.data["label_id"]:
        device_ids.extend(await _get_device_ids_from_label(hass, label_id))
    for area_id in call.data["area_id"]:
        device_ids.extend(await _get_device_ids_from_area(hass, area_id))

    seen: set[str] = set()
    unique_ids = [d for d in device_ids if not (d in seen or seen.add(d))]  # type: ignore[func-returns-value]
    if not unique_ids:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_targets_specified",
        )

    errors: list[str] = []
    for device_id in unique_ids:
        try:
            await _drawcustom_for_device(hass, device_id, call)
        except (HomeAssistantError, ServiceValidationError) as err:
            errors.append(f"{device_id}: {err}")
    if errors:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="multiple_errors",
            translation_placeholders={"errors": "\n".join(errors)},
        )


def _font_search_dirs(hass: HomeAssistant) -> list[str]:
    """Return font search directories in priority order."""
    candidates = [
        hass.config.path("www/fonts"),
        hass.config.path("media/fonts"),
        "/media/fonts",
    ]
    return [p for p in candidates if os.path.isdir(p)]


async def _drawcustom_for_device(
    hass: HomeAssistant, device_id: str, call: ServiceCall
) -> None:
    entry = _get_entry_for_device_id(hass, device_id)
    display = entry.runtime_data.device_config.displays[0]
    cs = display.color_scheme_enum
    color_scheme = cs if isinstance(cs, ColorScheme) else ColorScheme.from_value(cs)

    rotate: int = call.data["rotate"]
    # Keep generated dimensions aligned with effective display rotation so the
    # device-side fit does not rescale unexpectedly for 90/270 paths.
    base = display.rotation_enum
    base_deg = base.value if isinstance(base, Rotation) else 0
    if (base_deg + rotate) % 360 in (90, 270):
        gen_width, gen_height = display.pixel_height, display.pixel_width
    else:
        gen_width, gen_height = display.pixel_width, display.pixel_height

    img = await generate_image(
        width=gen_width,
        height=gen_height,
        elements=call.data["payload"],
        background=call.data["background"],
        accent_color=color_scheme.accent_color,
        session=async_get_clientsession(hass),
        data_provider=HADataProvider(hass),
        font_dirs=_font_search_dirs(hass),
    )

    if call.data["dry-run"]:
        _LOGGER.info("Drawcustom dry run for device %s", device_id)
        jpeg = await hass.async_add_executor_job(_pil_to_jpeg, img)
        async_dispatcher_send(hass, f"{SIGNAL_IMAGE_UPDATED}_{entry.unique_id}", jpeg)
        return

    dither_mode: DitherMode = call.data["dither"]
    refresh_mode: RefreshMode = call.data["refresh_type"]
    tone_compression_pct: float | None = call.data.get(ATTR_TONE_COMPRESSION)
    tone_compression: float | str = (
        tone_compression_pct / 100.0 if tone_compression_pct is not None else "auto"
    )

    pending = PendingDisplayUpload(
        image=img,
        dither_mode=dither_mode,
        refresh_mode=refresh_mode,
        tone=tone_compression,
        rotate=Rotation(rotate),
        source="drawcustom",
    )
    _LOGGER.info(
        "%s: drawcustom generated image "
        "(image_size=%s, refresh_mode=%s, dither_mode=%s)",
        entry.unique_id,
        getattr(img, "size", None),
        refresh_mode,
        dither_mode,
    )
    await _async_queue_or_send_image(hass, entry, pending)


async def _async_activate_led(call: ServiceCall) -> None:
    """Handle the activate_led service call."""
    entry = _get_entry_for_device(call)
    if not entry.runtime_data.device_config.leds:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_leds",
            translation_placeholders={"device_id": call.data[ATTR_DEVICE_ID]},
        )
    repeats: int = call.data["repeats"]
    flash_config = LedFlashConfig(
        mode=1,
        brightness=call.data["brightness"],
        step1=LedFlashStep(
            color=call.data["color1"],
            flash_count=call.data["flash_count1"],
            loop_delay_units=call.data["loop_delay1"],
            inter_delay_units=call.data["inter_delay1"],
        ),
        step2=LedFlashStep(
            color=call.data["color2"],
            flash_count=call.data["flash_count2"],
            loop_delay_units=call.data["loop_delay2"],
            inter_delay_units=call.data["inter_delay2"],
        ),
        step3=LedFlashStep(
            color=call.data["color3"],
            flash_count=call.data["flash_count3"],
            loop_delay_units=call.data["loop_delay3"],
            inter_delay_units=call.data["inter_delay3"],
        ),
        group_repeats=None if repeats == 0 else repeats,
    )
    instance: int = call.data["instance"]

    async def _led(device: OpenDisplayDevice) -> None:
        await device.activate_led(instance, flash_config)

    await _async_connect_and_run(call.hass, entry, _led)


async def _async_activate_buzzer(call: ServiceCall) -> None:
    """Handle the activate_buzzer service call."""
    entry = _get_entry_for_device(call)
    if not entry.runtime_data.device_config.buzzers:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_buzzers",
            translation_placeholders={"device_id": call.data[ATTR_DEVICE_ID]},
        )
    buzz_config = BuzzerActivateConfig.single_tone(
        frequency_hz=call.data["frequency_hz"],
        duration_ms=call.data["duration_ms"],
        repeats=call.data["repeats"],
    )
    instance: int = call.data["instance"]

    async def _buzz(device: OpenDisplayDevice) -> None:
        await device.activate_buzzer(instance, buzz_config)

    await _async_connect_and_run(call.hass, entry, _buzz)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register OpenDisplay services."""
    hass.services.async_register(
        DOMAIN,
        "upload_image",
        _async_upload_image,
        schema=SCHEMA_UPLOAD_IMAGE,
    )
    hass.services.async_register(
        DOMAIN,
        "drawcustom",
        _async_drawcustom,
        schema=SCHEMA_DRAWCUSTOM,
    )
    hass.services.async_register(
        DOMAIN,
        "activate_led",
        _async_activate_led,
        schema=SCHEMA_ACTIVATE_LED,
    )
    hass.services.async_register(
        DOMAIN,
        "activate_buzzer",
        _async_activate_buzzer,
        schema=SCHEMA_ACTIVATE_BUZZER,
    )
