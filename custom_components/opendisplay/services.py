"""Service registration for the OpenDisplay integration."""

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from datetime import datetime, timedelta, timezone
from enum import IntEnum
import functools
import io
import logging
import os
from typing import TYPE_CHECKING, Any

import aiohttp
from odl_renderer import generate_image
from opendisplay import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    ColorScheme,
    DitherMode,
    FitMode,
    LedFlashConfig,
    LedFlashStep,
    BuzzerActivateConfig,
    OpenDisplayDevice,
    OpenDisplayError,
    PartialState,
    RefreshMode,
    Rotation,
    prepare_image,
)
from PIL import Image as PILImage, ImageOps
import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothReachabilityIntent,
    async_address_reachability_diagnostics,
    async_ble_device_from_address,
)
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_source import async_resolve_media
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import get_url
from homeassistant.helpers.selector import MediaSelector, MediaSelectorConfig

if TYPE_CHECKING:
    from . import OpenDisplayConfigEntry

from .const import (
    CONF_BLOCKS_PER_ACK,
    CONF_ENCRYPTION_KEY,
    CONF_MAX_QUEUE_SIZE,
    DEFAULT_BLOCKS_PER_ACK,
    DEFAULT_MAX_QUEUE_SIZE,
    DOMAIN,
    SIGNAL_IMAGE_UPDATED,
)
from .delivery import DELIVERY_DEADLINE_S, DeliveryReceipt

ATTR_IMAGE = "image"
ATTR_ROTATION = "rotation"
ATTR_DITHER_MODE = "dither_mode"
ATTR_REFRESH_MODE = "refresh_mode"
ATTR_FIT_MODE = "fit_mode"
ATTR_TONE_COMPRESSION = "tone_compression"
ATTR_USE_MEASURED_PALETTES = "measured_palette"
ATTR_EXPIRE = "expire"


def _str_to_int_enum(enum_class: type[IntEnum]) -> Callable[[str], Any]:
    """Convert a lowercase enum name string to an enum member."""
    members = {m.name.lower(): m for m in enum_class}

    def validate(value: str) -> IntEnum:
        if (result := members.get(value)) is None:
            raise vol.Invalid(f"Invalid value: {value}")
        return result

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
    """Accept names ("full"/"fast"/"partial") and legacy numeric values.

    Legacy value 3 ("partial2"/full-frame partial) maps to PARTIAL: the library
    reads the panel's partial_update_support config and expands the region to
    the full frame automatically where required, so the distinction is obsolete.
    """
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.lstrip("-").isdigit()
    ):
        n = int(value)
        if n == 3:  # legacy partial2 / full-frame partial
            return RefreshMode.PARTIAL
        try:
            mode = RefreshMode(n)
        except ValueError as err:
            raise vol.Invalid(f"Invalid refresh_type: {value}") from err
        return mode
    return _str_to_int_enum(RefreshMode)(value)


SCHEMA_UPLOAD_IMAGE = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_IMAGE): vol.Any(
            cv.url, MediaSelector(MediaSelectorConfig(accept=["image/*"]))
        ),
        vol.Optional(ATTR_ROTATION, default=Rotation.ROTATE_0): vol.All(
            vol.Coerce(int), vol.Coerce(Rotation)
        ),
        vol.Optional(ATTR_DITHER_MODE, default="burkes"): _str_to_int_enum(DitherMode),
        vol.Optional(ATTR_REFRESH_MODE, default="full"): _refresh_type_value,
        vol.Optional(ATTR_FIT_MODE, default="contain"): _str_to_int_enum(FitMode),
        vol.Optional(ATTR_TONE_COMPRESSION): vol.All(
            vol.Coerce(float), vol.Range(min=0.0, max=100.0)
        ),
        vol.Optional(ATTR_USE_MEASURED_PALETTES, default=True): cv.boolean,
        vol.Optional(ATTR_EXPIRE): cv.positive_time_period,
    }
)


SCHEMA_DRAWCUSTOM = vol.Schema(
    {
        vol.Optional("device_id", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("label_id", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("area_id", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Required("payload"): list,
        vol.Optional("background", default="white"): cv.string,
        vol.Optional("rotate", default=0): vol.All(vol.Coerce(int), vol.In([0, 90, 180, 270])),
        vol.Optional("dither", default="burkes"): _dither_value,
        vol.Optional("refresh_type", default="full"): _refresh_type_value,
        vol.Optional(ATTR_TONE_COMPRESSION): vol.All(
            vol.Coerce(float), vol.Range(min=0.0, max=100.0)
        ),
        vol.Optional(ATTR_USE_MEASURED_PALETTES, default=False): cv.boolean,
        vol.Optional(ATTR_EXPIRE): cv.positive_time_period,
        vol.Optional("dry-run", default=False): cv.boolean,
    },
    extra=vol.REMOVE_EXTRA,  # silently drop legacy keys (ttl, preload_type, preload_lut, ...)
)


def _rgb_to_led_color(value: list[int]) -> int:
    """Convert [R, G, B] (0-255 each) to packed 8-bit LED color byte (3R 3G 2B)."""
    r, g, b = value
    return ((round(r * 7 / 255)) << 5) | ((round(g * 7 / 255)) << 2) | (round(b * 3 / 255))


def _ms_to_loop_delay(value: int) -> int:
    """Convert milliseconds to 4-bit loop delay units (×100 ms each, 0–1500 ms)."""
    return max(0, min(15, round(value / 100)))


def _ms_to_inter_delay(value: int) -> int:
    """Convert milliseconds to 8-bit inter-delay units (×100 ms each, 0–25500 ms)."""
    return max(0, min(255, round(value / 100)))


def _led_step_fields(n: int, *, color_default: list[int], flash_count_default: int) -> dict:
    """Return the voluptuous field definitions for one LED step."""
    return {
        vol.Optional(f"color{n}", default=color_default): _rgb_to_led_color,
        vol.Optional(f"flash_count{n}", default=flash_count_default): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=15)
        ),
        vol.Optional(f"loop_delay{n}", default=0): vol.All(vol.Coerce(int), _ms_to_loop_delay),
        vol.Optional(f"inter_delay{n}", default=0): vol.All(vol.Coerce(int), _ms_to_inter_delay),
    }


SCHEMA_ACTIVATE_LED = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional("instance", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Optional("brightness", default=8): vol.All(vol.Coerce(int), vol.Range(min=1, max=16)),
        vol.Optional("repeats", default=1): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        **_led_step_fields(1, color_default=[255, 0, 0], flash_count_default=1),
        **_led_step_fields(2, color_default=[0, 255, 0], flash_count_default=0),
        **_led_step_fields(3, color_default=[0, 0, 255], flash_count_default=0),
    }
)

SCHEMA_ACTIVATE_BUZZER = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional("instance", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=3)),
        vol.Optional("frequency_hz", default=1000): vol.All(vol.Coerce(int), vol.Range(min=0, max=12000)),
        vol.Optional("duration_ms", default=100): vol.All(vol.Coerce(int), vol.Range(min=5, max=1275)),
        vol.Optional("repeats", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
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
            translation_key="config_entry_not_found",
            translation_placeholders={"address": mac_address},
        )

    return entry


def _pil_to_jpeg(img: PILImage.Image) -> bytes:
    """Encode a PIL image as JPEG bytes."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


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
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            data = await resp.read()
    except aiohttp.ClientError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="media_download_error",
            translation_placeholders={"error": str(err)},
        ) from err

    return await hass.async_add_executor_job(_load_image_from_bytes, data)


class _DeviceUnavailable(Exception):
    """Signals the tag was dark or dropped the link during a live send.

    Raised only when ``reraise_ble=True`` so the caller can queue the content
    for the next wake instead of surfacing an ``upload_error``.
    """


# Probe budget for a probably-asleep tag: one connect attempt, short timeout.
# 5 s is >2x the observed 1-3 s connect-during-window latency (plus proxy
# slack), so a genuinely awake tag connects comfortably, while a dark ESP32
# (radio fully off in timer deep sleep) costs at most ~5 s before queuing —
# vs the ~40 s default budget (4 attempts x 10 s). Deliberately below both the
# 10 s wake window and the 15 s freshness horizon (wake_window_s +
# FRESHNESS_SLACK_S), so a probe triggered by a just-missed advert still lands
# inside the window it is betting on.
PROBE_CONNECT_TIMEOUT_S = 5.0
PROBE_MAX_ATTEMPTS = 1


async def _async_connect_and_run(
    hass: HomeAssistant,
    entry: "OpenDisplayConfigEntry",
    action: Callable[[OpenDisplayDevice], Awaitable[None]],
    use_measured_palettes: bool = False,
    reraise_ble: bool = False,
    *,
    connect_timeout: float | None = None,
    max_attempts: int | None = None,
) -> None:
    """Resolve BLE device, open a connection, run action, handle auth errors.

    When ``reraise_ble`` is set, a missing connectable device or a BLE
    connect/timeout failure raises ``_DeviceUnavailable`` instead of a
    translated ``device_not_found``/``upload_error``, so the caller can defer
    the work to the delivery queue. ``connect_timeout``/``max_attempts``
    override the library's connect budget (10 s x 4 attempts) — used by the
    probe path to bound a likely-doomed attempt; ``None`` keeps the library
    defaults. All other behavior is unchanged.
    """
    address = entry.unique_id
    assert address is not None
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        if reraise_ble:
            raise _DeviceUnavailable
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
    if raw_key is not None and len(raw_key) != 32:
        _LOGGER.error(
            "%s: stored encryption key is malformed (bad length); reauthentication required",
            address,
        )
        entry.async_start_reauth(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="authentication_error"
        )
    try:
        encryption_key = bytes.fromhex(raw_key) if raw_key is not None else None
    except ValueError as err:
        _LOGGER.error(
            "%s: stored encryption key is malformed (not hex); reauthentication required",
            address,
        )
        entry.async_start_reauth(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="authentication_error"
        ) from err

    device_kwargs: dict[str, Any] = {}
    if connect_timeout is not None:
        device_kwargs["timeout"] = connect_timeout
    if max_attempts is not None:
        device_kwargs["max_attempts"] = max_attempts
    # Sliding-window pipe-transfer tuning (max_queue_size == 1 disables it).
    options = entry.options
    device_kwargs["blocks_per_ack"] = options.get(
        CONF_BLOCKS_PER_ACK, DEFAULT_BLOCKS_PER_ACK
    )
    device_kwargs["max_queue_size"] = options.get(
        CONF_MAX_QUEUE_SIZE, DEFAULT_MAX_QUEUE_SIZE
    )

    try:
        # Serialize all BLE access to this tag: the device has a single BLE link
        # and the library has no per-address lock, so overlapping connections
        # (two automations, or a drawcustom racing an LED/buzzer/upload on the
        # same MAC) fail with a confusing upload_error. The lock is held for the
        # full connection lifetime and released on error. Held per config entry,
        # i.e. per MAC, so different tags are not serialized against each other.
        # Same wall-clock ceiling as the queued-delivery drain: without it a
        # wedged transfer would hold ble_lock forever and block every later
        # operation on this MAC (the library's per-read timeouts bound normal
        # failures, but not adversarial/buggy-firmware frame streams).
        async with (
            asyncio.timeout(DELIVERY_DEADLINE_S),
            entry.runtime_data.ble_lock,
            OpenDisplayDevice(
                mac_address=address,
                ble_device=ble_device,
                config=entry.runtime_data.device_config,
                use_measured_palettes=use_measured_palettes,
                encryption_key=encryption_key,
                **device_kwargs,
            ) as device,
        ):
            await action(device)
    except TimeoutError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="upload_error",
            translation_placeholders={
                "error": f"operation exceeded {DELIVERY_DEADLINE_S:.0f}s deadline"
            },
        ) from err
    except (AuthenticationFailedError, AuthenticationRequiredError) as err:
        _LOGGER.warning(
            "%s: device rejected the encryption key (%s); reauthentication required",
            address,
            err,
        )
        entry.async_start_reauth(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="authentication_error"
        ) from err
    except (BLEConnectionError, BLETimeoutError) as err:
        if reraise_ble:
            raise _DeviceUnavailable from err
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


async def _async_send_image(
    hass: HomeAssistant,
    entry: "OpenDisplayConfigEntry",
    img: PILImage.Image,
    *,
    device_id: str | None = None,
    dither_mode: DitherMode,
    refresh_mode: RefreshMode,
    fit: FitMode = FitMode.CONTAIN,
    tone: float | str = "auto",
    rotate: Rotation = Rotation.ROTATE_0,
    use_measured_palettes: bool = False,
    expire_seconds: float | None,
) -> DeliveryReceipt:
    """Upload a PIL image, delivering live or queuing it for the next wake.

    Returns a receipt describing whether the frame was delivered immediately or
    queued (for a sleeping device). Non-sleepy devices always deliver live and
    keep the original strict-failure behavior.
    """
    # Split the upload into its heavy CPU half and its BLE-I/O half. The CPU
    # work (rotate + fit + dither + encode + zlib on a full frame) is offloaded
    # to an executor thread so it never blocks the event loop; only the BLE
    # transfer runs on the loop. This mirrors what device.upload_image() does
    # internally, but that call ran _prepare_image synchronously on the loop.
    config = entry.runtime_data.device_config
    display_cfg = config.displays[0] if config and config.displays else None
    # Match upload_image(): only ask prepare_image() to build compressed data
    # when the panel accepts compressed uploads (plain ZIP bit or the
    # streaming-decompression bit). upload_prepared_image() then falls back to
    # the uncompressed protocol when compressed_data is None.
    supports_compression = (
        (display_cfg.supports_zip or display_cfg.supports_streaming_decompression)
        if display_cfg
        else True
    )
    prepared = await hass.async_add_executor_job(
        functools.partial(
            prepare_image,
            img,
            config=config,
            dither_mode=dither_mode,
            compress=supports_compression,
            tone=tone,
            fit=fit,
            rotate=rotate,
        )
    )

    # Partial refreshes diff against the entry's tracked frame; full/fast
    # refreshes re-baseline the panel, so start a fresh state that this upload
    # seeds (etag + frame) for the next partial. The library handles all
    # fallbacks (unsupported panel, etag mismatch, firmware NACK) and expands
    # the region to the full frame on panels that require it
    # (partial_update_support=2, see OpenDisplay/Firmware#80).
    runtime = entry.runtime_data
    if refresh_mode is not RefreshMode.PARTIAL:
        runtime.partial_state = PartialState()
    state = runtime.partial_state

    # The preview JPEG is built up-front so a queued frame can be shown on the
    # image entity immediately (D6), not only after a successful delivery.
    jpeg = await hass.async_add_executor_job(_pil_to_jpeg, img)

    profile = runtime.sleep_profile
    manager = runtime.delivery
    sleepy = manager is not None and profile.is_sleepy

    def _queue() -> DeliveryReceipt:
        assert manager is not None
        return manager.submit_upload(
            prepared=prepared,
            refresh_mode=refresh_mode,
            partial_state=state,
            use_measured_palettes=use_measured_palettes,
            preview_jpeg=jpeg,
            device_id=device_id,
            expire_seconds=expire_seconds,
        )

    async def _upload(device: OpenDisplayDevice) -> None:
        await device.upload_prepared_image(
            prepared, refresh_mode=refresh_mode, state=state
        )

    # Freshness gate: a probably-asleep tag will not usually answer a live
    # connect. Instead of queuing blind, spend one short connect attempt (the
    # "probe") in case the tag is actually awake: its wake adverts may have
    # been missed by the scanner, and Silabs tags advertise continuously even
    # when their power config looks sleepy. A dark ESP32 costs at most
    # ~PROBE_CONNECT_TIMEOUT_S before falling back to the queue. HA drops the
    # connectable BLEDevice ~3-5 min after the last advert, so a long-dark or
    # never-seen tag short-circuits to the queue at near-zero cost (no
    # BLEDevice -> _DeviceUnavailable without any radio traffic).
    probing = False
    connect_kwargs: dict[str, Any] = {}
    if sleepy:
        last_seen = (
            runtime.coordinator.data.last_seen if runtime.coordinator.data else None
        )
        if profile.probably_asleep(last_seen):
            if not profile.probe_before_queue:
                return _queue()
            probing = True
            connect_kwargs = {
                "connect_timeout": PROBE_CONNECT_TIMEOUT_S,
                "max_attempts": PROBE_MAX_ATTEMPTS,
            }

    try:
        await _async_connect_and_run(
            hass,
            entry,
            _upload,
            use_measured_palettes=use_measured_palettes,
            reraise_ble=sleepy,
            **connect_kwargs,
        )
    except _DeviceUnavailable:
        # The device was dark, dropped, or refused the link; defer.
        receipt = _queue()
        if probing:
            # Race: a wake advert arriving DURING the failed probe found no
            # pending work (nothing queued yet), so no drain started. If the
            # tag now looks fresh, kick a drain instead of waiting out a full
            # sleep cycle. notify_device_seen is a no-op while one is running.
            last_seen = (
                runtime.coordinator.data.last_seen if runtime.coordinator.data else None
            )
            if not profile.probably_asleep(last_seen):
                assert manager is not None
                manager.notify_device_seen("post-probe")
        return receipt

    async_dispatcher_send(hass, f"{SIGNAL_IMAGE_UPDATED}_{entry.unique_id}", jpeg)
    return DeliveryReceipt(status="delivered", expires_at=None)


def _receipt_response(receipt: DeliveryReceipt) -> ServiceResponse:
    """Build the service response payload from a delivery receipt."""
    expires_at = (
        datetime.fromtimestamp(receipt.expires_at, tz=timezone.utc).isoformat()
        if receipt.expires_at is not None
        else None
    )
    return {"status": receipt.status, "expires_at": expires_at}


def _expire_seconds(entry: "OpenDisplayConfigEntry", call: ServiceCall) -> float | None:
    """Resolve the service expiry option to seconds, or None for no expiry."""
    expire = call.data.get(ATTR_EXPIRE)
    if expire is None:
        return entry.runtime_data.sleep_profile.queue_timeout_s
    seconds = expire.total_seconds()
    return None if seconds == 0 else seconds


async def _async_upload_image(call: ServiceCall) -> ServiceResponse:
    """Handle the upload_image service call."""
    entry = _get_entry_for_device(call)

    device_id: str = call.data[ATTR_DEVICE_ID]
    image_data: dict[str, Any] | str = call.data[ATTR_IMAGE]
    rotation: Rotation = call.data[ATTR_ROTATION]
    dither_mode: DitherMode = call.data[ATTR_DITHER_MODE]
    refresh_mode: RefreshMode = call.data[ATTR_REFRESH_MODE]
    fit_mode: FitMode = call.data[ATTR_FIT_MODE]
    tone_compression_pct: float | None = call.data.get(ATTR_TONE_COMPRESSION)
    tone_compression: float | str = (
        tone_compression_pct / 100.0 if tone_compression_pct is not None else "auto"
    )
    use_measured_palettes: bool = call.data[ATTR_USE_MEASURED_PALETTES]
    expire_seconds = _expire_seconds(entry, call)

    # A plain URL (e.g. an automation pushing a rendered snapshot) must be
    # explicitly allowlisted; media-source items are already trusted.
    if isinstance(image_data, str) and not call.hass.config.is_allowed_external_url(
        image_data
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="url_not_allowed",
            translation_placeholders={"url": image_data},
        )

    # Latest-wins for upload_image specifically: a newer upload cancels an
    # older still-running one instead of queuing behind it (an automation
    # pushing a fresh snapshot supersedes the stale one). This composes with the
    # per-MAC ble_lock without deadlocking: the cancel + await below happens
    # before _async_send_image acquires the lock, so the cancelled task releases
    # the lock (its `async with ble_lock` unwinds) before this one takes it.
    current = asyncio.current_task()
    if (prev := entry.runtime_data.upload_task) is not None and not prev.done():
        prev.cancel()
        # pylint: disable-next=home-assistant-action-swallowed-exception
        with contextlib.suppress(asyncio.CancelledError):
            await prev
    entry.runtime_data.upload_task = current

    try:
        if isinstance(image_data, str):
            pil_image = await _async_download_image(call.hass, image_data)
        else:
            media = await async_resolve_media(
                call.hass, image_data["media_content_id"], None
            )
            if media.path is not None:
                pil_image = await call.hass.async_add_executor_job(
                    _load_image, str(media.path)
                )
            else:
                pil_image = await _async_download_image(call.hass, media.url)

        receipt = await _async_send_image(
            call.hass,
            entry,
            pil_image,
            device_id=device_id,
            dither_mode=dither_mode,
            refresh_mode=refresh_mode,
            fit=fit_mode,
            tone=tone_compression,
            rotate=rotation,
            use_measured_palettes=use_measured_palettes,
            expire_seconds=expire_seconds,
        )
        return _receipt_response(receipt)
    except asyncio.CancelledError:
        # Superseded by a newer upload (latest-wins); report it honestly rather
        # than surfacing the cancellation.
        return {"status": "superseded", "expires_at": None}
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
            translation_key="config_entry_not_found",
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


async def _async_drawcustom(call: ServiceCall) -> ServiceResponse:
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
    receipts: list[DeliveryReceipt] = []
    results: dict[str, Any] = {}
    for device_id in unique_ids:
        try:
            receipt = await _drawcustom_for_device(hass, device_id, call)
        except (HomeAssistantError, ServiceValidationError) as err:
            errors.append(f"{device_id}: {err}")
        else:
            receipts.append(receipt)
            results[device_id] = _receipt_response(receipt)
    if errors:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="multiple_errors",
            translation_placeholders={"errors": "\n".join(errors)},
        )

    # Summarize across all targets: any queued device makes the batch "queued"
    # (with the soonest finite expiry, or None if all queued uploads are infinite);
    # otherwise delivered (or dry_run).
    queued = [r for r in receipts if r.status == "queued"]
    if queued:
        status = "queued"
        finite_expiries = [r.expires_at for r in queued if r.expires_at is not None]
        expires_epoch: float | None = min(finite_expiries) if finite_expiries else None
    elif receipts and all(r.status == "dry_run" for r in receipts):
        status = "dry_run"
        expires_epoch = None
    else:
        status = "delivered"
        expires_epoch = None
    expires_at = (
        datetime.fromtimestamp(expires_epoch, tz=timezone.utc).isoformat()
        if expires_epoch is not None
        else None
    )
    return {"status": status, "expires_at": expires_at, "results": results}


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
) -> DeliveryReceipt:
    entry = _get_entry_for_device_id(hass, device_id)
    display = entry.runtime_data.device_config.displays[0]
    cs = display.color_scheme_enum
    color_scheme = cs if isinstance(cs, ColorScheme) else ColorScheme.from_value(cs)

    rotate: int = call.data["rotate"]
    # The payload is authored against the final on-screen orientation. The device
    # applies (base + rotate) and fits the result to its native pixel grid, so when
    # the effective rotation transposes the axes (90/270) we render the canvas
    # transposed too. That keeps the device-side fit a 1:1 no-op instead of
    # scaling/centering a mismatched-aspect image. Rotation itself is left to the
    # device (consistent with the upload_image path) rather than rotating here.
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
        font_dirs=await hass.async_add_executor_job(_font_search_dirs, hass),
    )

    if call.data["dry-run"]:
        _LOGGER.info("Drawcustom dry run for device %s", device_id)
        jpeg = await hass.async_add_executor_job(_pil_to_jpeg, img)
        async_dispatcher_send(hass, f"{SIGNAL_IMAGE_UPDATED}_{entry.unique_id}", jpeg)
        return DeliveryReceipt(status="dry_run", expires_at=None)

    dither_mode: DitherMode = call.data["dither"]
    refresh_mode: RefreshMode = call.data["refresh_type"]
    tone_compression_pct: float | None = call.data.get(ATTR_TONE_COMPRESSION)
    tone_compression: float | str = (
        tone_compression_pct / 100.0 if tone_compression_pct is not None else "auto"
    )
    use_measured_palettes: bool = call.data[ATTR_USE_MEASURED_PALETTES]
    expire_seconds = _expire_seconds(entry, call)

    return await _async_send_image(
        hass,
        entry,
        img,
        device_id=device_id,
        dither_mode=dither_mode,
        refresh_mode=refresh_mode,
        tone=tone_compression,
        rotate=Rotation(rotate),
        use_measured_palettes=use_measured_palettes,
        expire_seconds=expire_seconds,
    )


def _raise_if_sleeping(entry: "OpenDisplayConfigEntry", device_id: str) -> None:
    """Fail fast for an immediate-only action when the tag is provably asleep.

    LED/buzzer notifications that fire hours late are worse than an error, so a
    sleeping device rejects them immediately rather than queuing.
    """
    runtime = entry.runtime_data
    profile = runtime.sleep_profile
    if not profile.is_sleepy:
        return
    last_seen = runtime.coordinator.data.last_seen if runtime.coordinator.data else None
    if profile.probably_asleep(last_seen):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="device_sleeping",
            translation_placeholders={"device_id": device_id},
        )


async def _async_activate_led(call: ServiceCall) -> None:
    """Handle the activate_led service call."""
    entry = _get_entry_for_device(call)
    if not entry.runtime_data.device_config.leds:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_leds",
            translation_placeholders={"device_id": call.data[ATTR_DEVICE_ID]},
        )
    _raise_if_sleeping(entry, call.data[ATTR_DEVICE_ID])
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
    _raise_if_sleeping(entry, call.data[ATTR_DEVICE_ID])
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
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "drawcustom",
        _async_drawcustom,
        schema=SCHEMA_DRAWCUSTOM,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, "activate_led", _async_activate_led, schema=SCHEMA_ACTIVATE_LED)
    hass.services.async_register(DOMAIN, "activate_buzzer", _async_activate_buzzer, schema=SCHEMA_ACTIVATE_BUZZER)
