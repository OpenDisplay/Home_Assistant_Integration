"""Service registration for the OpenDisplay integration."""

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from datetime import UTC, datetime, timedelta
from enum import IntEnum
import functools
import io
import logging
import os
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp
from homeassistant.components.bluetooth import (
    BluetoothReachabilityIntent,
    async_address_reachability_diagnostics,
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
from homeassistant.helpers.template import Template, TemplateError, is_template_string
from odl_renderer import generate_image
from PIL import Image as PILImage, ImageOps
import voluptuous as vol

from opendisplay import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    BuzzerActivateConfig,
    ColorScheme,
    DitherMode,
    FitMode,
    LedFlashConfig,
    LedFlashStep,
    NfcNotSupportedError,
    NfcRecordType,
    NfcWriteError,
    OpenDisplayDevice,
    OpenDisplayError,
    PartialState,
    RefreshMode,
    Rotation,
    prepare_image,
)

if TYPE_CHECKING:
    from . import OpenDisplayConfigEntry

from .ble_lock import ble_connection
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
from .transport import async_run_with_fallback

ATTR_IMAGE = "image"
ATTR_ROTATION = "rotation"
ATTR_DITHER_MODE = "dither_mode"
ATTR_REFRESH_MODE = "refresh_mode"
ATTR_FIT_MODE = "fit_mode"
ATTR_TONE_COMPRESSION = "tone_compression"
ATTR_USE_MEASURED_PALETTES = "measured_palette"


def tone_and_measured_palettes_from_call_data(
    call_data: dict[str, Any],
) -> tuple[float | str, bool]:
    """Derive prepare_image's tone/use_measured_palettes from a validated call.

    Shared by both service handlers that call prepare_image
    (_async_upload_image, _drawcustom_for_device) so the one formula lives
    in one place -- and by the designer render endpoint's own test
    (tests/test_designer_render.py), which validates a call carrying
    neither tone_compression nor measured_palette against the real
    SCHEMA_DRAWCUSTOM and asserts the render endpoint's prepare_image call
    matches THIS function's output, not a hardcoded literal duplicated
    independently of it. A future change to either the schema's defaults or
    this formula moves that test's expectation with it, instead of the test
    silently drifting from what the send path actually does.
    """
    tone_pct: float | None = call_data.get(ATTR_TONE_COMPRESSION)
    tone: float | str = tone_pct / 100.0 if tone_pct is not None else "auto"
    use_measured_palettes: bool = call_data[ATTR_USE_MEASURED_PALETTES]
    return tone, use_measured_palettes


def _render_template_node(hass: HomeAssistant, value: Any) -> Any:
    """Recursively render HA templates in a payload element's field values."""
    if isinstance(value, str):
        if is_template_string(value):
            return Template(value, hass).async_render(parse_result=True)
        return value
    if isinstance(value, list):
        return [_render_template_node(hass, item) for item in value]
    if isinstance(value, dict):
        return {key: _render_template_node(hass, item) for key, item in value.items()}
    return value


def render_payload_templates(hass: HomeAssistant, payload: list[Any]) -> list[Any]:
    """Render Home Assistant templates in every drawcustom payload element.

    docs/drawcustom/supported_types.md documents that "the integration ...
    additionally expands Home Assistant templates in field values" before
    odl-renderer sees them -- odl-renderer's own code agrees with that
    premise (its comments say "Home Assistant templates render to strings"
    before it's ever called), but no code anywhere in this integration
    actually did that rendering. (That citation is about what odl-renderer
    itself assumes its *caller* already resolved a template to, not a claim
    about this function's own output: ``async_render``'s default
    ``parse_result=True`` below natively types a simple literal result --
    e.g. a rendered ``False``/``12`` comes back as a Python ``bool``/``int``,
    not the string "False"/"12" -- verified safe against every field that
    matters here, including ``visible``, whose own ``_coerce_visible``
    (odl-renderer's ``core.py``) explicitly branches on ``bool`` as well as
    ``str``.) A literal ``{{ ... }}`` string reached
    odl-renderer's own icon/text lookup unevaluated (tier-1 adversarial
    review, reproduced live with the designer's Load Demo payload's
    templated icon field: ``Icon '{{ iif(...) }}' not found``, both via
    "Send to display" and the designer render endpoint -- neither call site
    ever rendered templates, so both failed identically).

    Fixed in exactly ONE shared place, called by both
    ``_drawcustom_for_device`` (the send path) and the designer's render
    endpoint (``designer/render.py``), so a caller can never observe one
    behaving differently from the other. Each string field value that
    contains template syntax (``is_template_string``) is rendered through
    hass's own ``Template`` helper; everything else passes through
    unchanged, exactly as if the caller had already resolved it -- odl-
    renderer explicitly tolerates receiving already-resolved plain strings
    for numeric fields too (see its own ``coordinates.py`` comment), so no
    additional type coercion is done here beyond what ``Template.
    async_render`` already does by default.

    A template that raises (a broken reference -- HA's own template
    functions like ``is_state_attr``/``is_state`` degrade to a sensible
    default for a merely-missing entity rather than raising, so this is not
    the common "state doesn't exist yet" case) becomes a
    ``ServiceValidationError`` naming the offending element's list index
    and ``type``, instead of either a silent unevaluated literal reaching
    odl-renderer (the original bug) or an opaque traceback from inside
    odl-renderer's own icon/text lookup.
    """
    rendered: list[Any] = []
    for index, element in enumerate(payload):
        try:
            rendered.append(_render_template_node(hass, element))
        except TemplateError as err:
            element_type = (
                element.get("type", "?") if isinstance(element, dict) else "?"
            )
            raise ServiceValidationError(
                f"drawcustom payload element {index} (type '{element_type}') has an"
                f" invalid template: {err}"
            ) from err
    return rendered


ATTR_RECORD_TYPE = "record_type"
ATTR_CONTENT = "content"
ATTR_MIME_TYPE = "mime_type"

# Maximum NDEF record body the firmware accepts (matches the library's
# client-side ValueError threshold); enforced here too so an oversized
# payload is rejected before spending a BLE connection on it.
NFC_MAX_PAYLOAD = 512
HA_TAG_URL_PREFIX = "https://www.home-assistant.io/tag/"

# HA-facing record_type strings mapped to the library's NDEF record enum,
# used only for debug logging; the write_nfc_* device methods already pick
# the right NfcRecordType internally.
_NFC_RECORD_TYPE_ENUM: dict[str, NfcRecordType] = {
    "url": NfcRecordType.URI,
    "text": NfcRecordType.TEXT,
    "mime": NfcRecordType.MIME,
}


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


def _valid_melody(value: str) -> str:
    """Validate a compact melody string via the py-opendisplay parser.

    Parses ``value`` with default tempo/duration settings purely to check token
    syntax, converting the parser's ``ValueError`` into ``vol.Invalid`` (carrying
    the offending token's position and text). Tempo-dependent duration overflow
    cannot be caught here because ``tempo`` is a sibling field; the handler
    re-parses with the real values and re-raises as ``invalid_melody``.
    """
    try:
        BuzzerActivateConfig.melody(value)
    except ValueError as err:
        raise vol.Invalid(str(err)) from err
    return value


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
            vol.Coerce(int), vol.In([0, 90, 180, 270])
        ),
        vol.Optional("dither", default="burkes"): _dither_value,
        vol.Optional("refresh_type", default="full"): _refresh_type_value,
        vol.Optional(ATTR_TONE_COMPRESSION): vol.All(
            vol.Coerce(float), vol.Range(min=0.0, max=100.0)
        ),
        vol.Optional(ATTR_USE_MEASURED_PALETTES, default=False): cv.boolean,
        vol.Optional("dry-run", default=False): cv.boolean,
    },
    extra=vol.REMOVE_EXTRA,  # silently drop legacy keys (ttl, preload_type, preload_lut, ...)
)


def _rgb_to_led_color(value: list[int]) -> int:
    """Convert [R, G, B] (0-255 each) to packed 8-bit LED color byte (3R 3G 2B)."""
    r, g, b = value
    return (
        ((round(r * 7 / 255)) << 5) | ((round(g * 7 / 255)) << 2) | (round(b * 3 / 255))
    )


def _ms_to_loop_delay(value: int) -> int:
    """Convert milliseconds to 4-bit loop delay units (100 ms each, 0-1500 ms)."""
    return max(0, min(15, round(value / 100)))


def _ms_to_inter_delay(value: int) -> int:
    """Convert milliseconds to 8-bit inter-delay units (100 ms each, 0-25500 ms)."""
    return max(0, min(255, round(value / 100)))


def _led_step_fields(
    n: int, *, color_default: list[int], flash_count_default: int
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

SCHEMA_WRITE_NFC = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_RECORD_TYPE, default="url"): vol.In(
            ["url", "text", "mime", "ha_tag"]
        ),
        vol.Required(ATTR_CONTENT): cv.string,
        vol.Optional(ATTR_MIME_TYPE): cv.string,
    }
)

SCHEMA_PLAY_MELODY = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional("instance", default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=3)
        ),
        vol.Required("notes"): vol.All(cv.string, _valid_melody),
        vol.Optional("tempo", default=120): vol.All(
            vol.Coerce(int), vol.Range(min=40, max=400)
        ),
        vol.Optional("repeats", default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=255)
        ),
        vol.Optional("default_note_ms", default=200): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=1275)
        ),
        vol.Optional("default_length"): vol.All(
            vol.Coerce(int), vol.In([1, 2, 4, 8, 16, 32])
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


# media_source resolves these two domains to their *_proxy_stream sibling: an
# endless multipart feed, because it resolves for media players, which want
# something to keep playing. A panel wants the frame that is current now.
_STILL_ENDPOINTS = {
    "camera": "/api/camera_proxy/{}",
    "image": "/api/image_proxy/{}",
}


def _still_endpoint(media_content_id: str) -> str | None:
    """Return the still-image path for a camera/image source, else None."""
    domain, _, entity_id = media_content_id.removeprefix("media-source://").partition(
        "/"
    )
    if (path := _STILL_ENDPOINTS.get(domain)) is None:
        return None
    return path.format(entity_id)


async def _async_download_image(hass: HomeAssistant, url: str) -> PILImage.Image:
    """Download an image from a URL and return a PIL Image."""
    if not url.startswith(("http://", "https://")):
        url = get_url(hass) + async_sign_path(
            hass, url, timedelta(minutes=5), use_content_user=True
        )
    session = async_get_clientsession(hass)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            data = await resp.read()
    except (aiohttp.ClientError, TimeoutError) as err:
        # TimeoutError is not a ClientError: aiohttp raises it bare when the
        # total timeout expires, so it needs naming here to be translated.
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
    entry: OpenDisplayConfigEntry,
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

    def _ble_unavailable() -> BaseException:
        """Build the error to raise when no connectable BLE device exists.

        Called by the transport helper only when BLE is the selected (or
        fallen-back-to) transport and the device is not connectable — a WiFi
        delivery that succeeds never reaches this.
        """
        if reraise_ble:
            return _DeviceUnavailable()
        return HomeAssistantError(
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

    base_kwargs: dict[str, Any] = {
        "config": entry.runtime_data.device_config,
        "use_measured_palettes": use_measured_palettes,
        "encryption_key": encryption_key,
    }
    if connect_timeout is not None:
        base_kwargs["timeout"] = connect_timeout
    if max_attempts is not None:
        base_kwargs["max_attempts"] = max_attempts
    # Sliding-window pipe-transfer tuning (max_queue_size == 1 disables it).
    options = entry.options
    base_kwargs["blocks_per_ack"] = options.get(
        CONF_BLOCKS_PER_ACK, DEFAULT_BLOCKS_PER_ACK
    )
    base_kwargs["max_queue_size"] = options.get(
        CONF_MAX_QUEUE_SIZE, DEFAULT_MAX_QUEUE_SIZE
    )

    try:
        # Serialize all access to this tag: the device has a single link (BLE or
        # the one-client WiFi/LAN endpoint) and the library has no per-address
        # lock, so overlapping connections (two automations, or a drawcustom
        # racing an LED/buzzer/upload on the same MAC) fail with a confusing
        # upload_error. The lock is MAC-keyed — hence transport-neutral — held for
        # the full connection lifetime and released on error, so different tags
        # are not serialized against each other.
        # Same wall-clock ceiling as the queued-delivery drain: without it a
        # wedged transfer would hold the lock forever and block every later
        # operation on this MAC (the library's per-read timeouts bound normal
        # failures, but not adversarial/buggy-firmware frame streams).
        # The transport helper prefers WiFi when the entry has a fresh mDNS host
        # and falls back to BLE on any WiFi failure, all inside this lock.
        async with (
            asyncio.timeout(DELIVERY_DEADLINE_S),
            ble_connection(address, "service call (upload/drawcustom/LED/buzzer)"),
        ):
            await async_run_with_fallback(
                hass,
                entry,
                action,
                base_kwargs=base_kwargs,
                ble_unavailable=_ble_unavailable,
            )
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


async def _prepare_for_device(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    img: PILImage.Image,
    *,
    dither_mode: DitherMode,
    fit: FitMode = FitMode.CONTAIN,
    tone: float | str = "auto",
    rotate: Rotation = Rotation.ROTATE_0,
    use_measured_palettes: bool = False,
) -> tuple[bytes, bytes | None, PILImage.Image]:
    """Turn a rendered image into this device's upload buffer, off the loop.

    The single place a frame becomes device-facing: rotate by
    (base + rotate), fit to the native pixel grid, dither against the
    panel's palette, encode, and optionally zlib. Returns `prepare_image`'s
    own `(image_data, compressed_data, processed_image)`.

    Shared by the real send (`_async_send_image`) and by `drawcustom`'s
    `dry-run`, so a dry run previews the SAME artifact a real send would
    upload rather than a separate rendering that merely resembles it
    (maintainer ruling: "dry run should be honest of course, otherwise it
    won't be a dry run"). Every argument that shapes the buffer is a
    parameter, and the one that isn't -- compression -- is derived from the
    device here, so neither caller can drift from the other by passing
    something different.

    The heavy CPU work (rotate + fit + dither + encode + zlib on a full
    frame) runs in an executor thread so it never blocks the event loop.
    This mirrors what `device.upload_image()` does internally, but that call
    ran `_prepare_image` synchronously on the loop.
    """
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
    return await hass.async_add_executor_job(
        functools.partial(
            prepare_image,
            img,
            config=config,
            dither_mode=dither_mode,
            compress=supports_compression,
            tone=tone,
            fit=fit,
            rotate=rotate,
            use_measured_palettes=use_measured_palettes,
        )
    )


async def _async_send_image(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    img: PILImage.Image,
    *,
    device_id: str | None = None,
    dither_mode: DitherMode,
    refresh_mode: RefreshMode,
    fit: FitMode = FitMode.CONTAIN,
    tone: float | str = "auto",
    rotate: Rotation = Rotation.ROTATE_0,
    use_measured_palettes: bool = False,
) -> DeliveryReceipt:
    """Upload a PIL image, delivering live or queuing it for the next wake.

    Returns a receipt describing whether the frame was delivered immediately or
    queued (for a sleeping device). Non-sleepy devices always deliver live and
    keep the original strict-failure behavior.
    """
    prepared = await _prepare_for_device(
        hass,
        entry,
        img,
        dither_mode=dither_mode,
        fit=fit,
        tone=tone,
        rotate=rotate,
        use_measured_palettes=use_measured_palettes,
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
    #
    # Built from `prepared[2]` -- the POST-rotation, post-fit, post-dither
    # buffer that is actually uploaded -- and NOT from `img`, the
    # pre-rotation logical surface it was rendered from (tier-2 round 3,
    # real hardware). The surface is identical for two opposite
    # orientations (90 and 270 differ only in which way the panel is turned
    # onto it), so a preview built from it looked perfect for BOTH while
    # exactly one of them was upside down on the wall -- the maintainer's
    # own report, and a feedback gap only a walk to the display could
    # close. `tests/test_rotation_parity.py` pins both halves: what each
    # orientation puts in the uploaded buffer, and that this preview is
    # that buffer.
    #
    # Deliberately NOT the designer's own preview endpoint
    # (`designer/render.py`), which returns the logical surface because the
    # designer's canvas IS that surface. Different artifact, different
    # audience, unchanged.
    jpeg = await hass.async_add_executor_job(_pil_to_jpeg, prepared[2])

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
        datetime.fromtimestamp(receipt.expires_at, tz=UTC).isoformat()
        if receipt.expires_at is not None
        else None
    )
    return {"status": receipt.status, "expires_at": expires_at}


async def _async_upload_image(call: ServiceCall) -> ServiceResponse:
    """Handle the upload_image service call."""
    entry = _get_entry_for_device(call)

    device_id: str = call.data[ATTR_DEVICE_ID]
    image_data: dict[str, Any] | str = call.data[ATTR_IMAGE]
    rotation: Rotation = call.data[ATTR_ROTATION]
    dither_mode: DitherMode = call.data[ATTR_DITHER_MODE]
    refresh_mode: RefreshMode = call.data[ATTR_REFRESH_MODE]
    fit_mode: FitMode = call.data[ATTR_FIT_MODE]
    tone_compression, use_measured_palettes = tone_and_measured_palettes_from_call_data(
        call.data
    )

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
        elif (still := _still_endpoint(image_data["media_content_id"])) is not None:
            pil_image = await _async_download_image(call.hass, still)
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
        """Initialize the provider."""
        self._hass = hass

    async def get_history(  # noqa: D102 - shape is fixed by odl_renderer
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
) -> OpenDisplayConfigEntry:
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
    # (with the soonest expiry); otherwise delivered (or dry_run).
    queued = [r for r in receipts if r.status == "queued" and r.expires_at is not None]
    if queued:
        status = "queued"
        expires_epoch: float | None = min(r.expires_at for r in queued)  # type: ignore[type-var]
    elif receipts and all(r.status == "dry_run" for r in receipts):
        status = "dry_run"
        expires_epoch = None
    else:
        status = "delivered"
        expires_epoch = None
    expires_at = (
        datetime.fromtimestamp(expires_epoch, tz=UTC).isoformat()
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


def _decode_local_image_sources(elements: list[Any]) -> list[Any]:
    """Return `elements` with local-file image sources decoded to PIL images.

    Runs in an executor -- see `preload_local_image_sources` for why.
    """
    decoded: list[Any] = []
    for element in elements:
        source = element.get("url") if isinstance(element, dict) else None
        # `dlimg` is the only element type that loads an image
        # (`odl_renderer.elements.media`), and `load_image`'s own ordering is
        # HTTP(S) first, then `data:`, then a leading `/` for a local file --
        # only the last of those opens a file.
        if (
            not isinstance(element, dict)
            or element.get("type") != "dlimg"
            or not isinstance(source, str)
            or not source.startswith("/")
        ):
            decoded.append(element)
            continue
        try:
            image = PILImage.open(source)
            image.load()
        except Exception:
            # Transparent on failure: leave the element exactly as it was so
            # `load_image` raises its own error, with its own message, at the
            # point it always did. This preload changes WHERE the file is
            # read, never WHETHER it is or what happens when it can't be.
            decoded.append(element)
            continue
        decoded.append({**element, "url": image})
    return decoded


async def preload_local_image_sources(
    hass: HomeAssistant, elements: list[Any]
) -> list[Any]:
    """Decode a payload's local image files off the event loop.

    `generate_image` is a coroutine awaited on the loop, but a `dlimg`
    element whose `url` is a local absolute path reaches
    `odl_renderer.media_loader._load_from_file`, which calls
    `PIL.Image.open` directly -- a blocking `open()` inside the event loop.
    Home Assistant's own detector reports it (verbatim, from real hardware:
    "Detected blocking call to open with args ('/media/pohl89-480h.png',
    'rb') inside the event loop by custom integration 'opendisplay'"), and
    https://developers.home-assistant.io/docs/asyncio_blocking_operations/
    says to move that work to an executor.

    `load_image` returns a `PIL.Image.Image` source AS-IS, so decoding here
    -- in an executor -- removes the file I/O from the loop without
    changing which file is read, how it is read, or what the renderer then
    does with it. Deliberately NOT restricted to any permitted root: this
    is the send/render pipeline's own resolution, and narrowing it would
    change what a working payload renders. (The designer's asset endpoint,
    which hands bytes to a browser, is the thing that needs a root policy;
    see `designer/asset.py`.)

    Remote (`http(s)://`) and `data:` sources are left alone: the first is
    already fetched asynchronously through the shared session, the second
    never touches the filesystem.

    Called by both `generate_image` call sites -- the send path
    (`_drawcustom_for_device`) and the designer's render endpoint -- so
    neither can regain the violation while the other is fixed.
    """
    return await hass.async_add_executor_job(_decode_local_image_sources, elements)


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
        elements=await preload_local_image_sources(
            hass, render_payload_templates(hass, call.data["payload"])
        ),
        background=call.data["background"],
        accent_color=color_scheme.accent_color,
        session=async_get_clientsession(hass),
        data_provider=HADataProvider(hass),
        font_dirs=await hass.async_add_executor_job(_font_search_dirs, hass),
    )

    dither_mode: DitherMode = call.data["dither"]
    tone_compression, use_measured_palettes = tone_and_measured_palettes_from_call_data(
        call.data
    )

    if call.data["dry-run"]:
        # "Dry run should be honest of course, otherwise it won't be a dry
        # run" (maintainer ruling). It therefore prepares the frame exactly
        # as the send below would -- same rotate, same dither, same tone and
        # measured-palette derivation, same device config, through the one
        # shared `_prepare_for_device` -- and publishes THAT buffer. It used
        # to publish `img`, the pre-rotation, un-dithered logical surface: a
        # picture of something no panel would ever be given, identical for
        # two opposite orientations, and blind to `dither` entirely.
        #
        # What it deliberately does NOT do is anything a real send does
        # beyond preparation: no upload, no queue entry, no partial-state
        # reset, no receipt but `dry_run`. Preparation is pure CPU and
        # touches neither the device nor the entry's delivery state.
        #
        # A preparation failure is left to propagate, and that is the point:
        # if `prepare_image` cannot build a buffer for this device, the real
        # send would fail the same way, so reporting it is the honest answer
        # and the caller learns it without sending. Swallowing it to keep
        # the old "dry runs always succeed" behavior would mean previewing a
        # frame that could never be delivered.
        _LOGGER.info("Drawcustom dry run for device %s", device_id)
        prepared = await _prepare_for_device(
            hass,
            entry,
            img,
            dither_mode=dither_mode,
            tone=tone_compression,
            rotate=Rotation(rotate),
            use_measured_palettes=use_measured_palettes,
        )
        jpeg = await hass.async_add_executor_job(_pil_to_jpeg, prepared[2])
        async_dispatcher_send(hass, f"{SIGNAL_IMAGE_UPDATED}_{entry.unique_id}", jpeg)
        return DeliveryReceipt(status="dry_run", expires_at=None)

    refresh_mode: RefreshMode = call.data["refresh_type"]

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
    )


def _raise_if_sleeping(entry: OpenDisplayConfigEntry, device_id: str) -> None:
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


async def _async_write_nfc(call: ServiceCall) -> None:
    """Handle the write_nfc service call.

    Writes an NDEF record (URL, text, or MIME) to the device's NFC tag.
    ``ha_tag`` is a convenience record_type: the content is treated as an
    Home Assistant tag id and composed into a home-assistant.io tag URL
    before being written as a url record.
    """
    device_id: str = call.data[ATTR_DEVICE_ID]
    entry = _get_entry_for_device(call)
    device_config = entry.runtime_data.device_config
    if device_config is None or not any(
        cfg.enabled for cfg in device_config.nfc_configs
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_nfc",
            translation_placeholders={"device_id": device_id},
        )
    _raise_if_sleeping(entry, device_id)

    record_type: str = call.data[ATTR_RECORD_TYPE]
    content: str = call.data[ATTR_CONTENT]
    mime_type: str | None = call.data.get(ATTR_MIME_TYPE)

    if mime_type is not None and record_type != "mime":
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="mime_type_not_applicable",
        )

    if not content.strip():
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="nfc_content_empty",
        )

    if record_type == "ha_tag":
        tag_id = content.strip()
        content = HA_TAG_URL_PREFIX + quote(tag_id, safe="")
        record_type = "url"

    effective_mime_type: str | None = None
    if record_type == "mime":
        effective_mime_type = mime_type or "text/vcard"
        mime_bytes = effective_mime_type.encode("utf-8")
        if not 1 <= len(mime_bytes) <= 255:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="nfc_mime_type_invalid",
                translation_placeholders={"mime_type": effective_mime_type},
            )
        size = 1 + len(mime_bytes) + len(content.encode("utf-8"))
    else:
        size = len(content.encode("utf-8"))

    if size > NFC_MAX_PAYLOAD:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="nfc_content_too_long",
            translation_placeholders={
                "size": str(size),
                "max": str(NFC_MAX_PAYLOAD),
            },
        )

    async def _nfc(device: OpenDisplayDevice) -> None:
        _LOGGER.debug(
            "%s: writing NFC %s record",
            device_id,
            _NFC_RECORD_TYPE_ENUM[record_type].name,
        )
        try:
            if record_type == "url":
                await device.write_nfc_url(content)
            elif record_type == "text":
                await device.write_nfc_text(content)
            else:  # mime
                assert effective_mime_type is not None
                await device.write_nfc_mime(effective_mime_type, content)
        except NfcNotSupportedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="nfc_not_supported",
                translation_placeholders={"device_id": device_id},
            ) from err
        except NfcWriteError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="nfc_write_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    await _async_connect_and_run(call.hass, entry, _nfc)


async def _async_play_melody(call: ServiceCall) -> None:
    """Handle the play_melody service call."""
    entry = _get_entry_for_device(call)
    if not entry.runtime_data.device_config.buzzers:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_buzzers",
            translation_placeholders={"device_id": call.data[ATTR_DEVICE_ID]},
        )
    _raise_if_sleeping(entry, call.data[ATTR_DEVICE_ID])
    try:
        buzz_config = BuzzerActivateConfig.melody(
            call.data["notes"],
            tempo=call.data["tempo"],
            repeats=call.data["repeats"],
            default_ms=call.data["default_note_ms"],
            default_length=call.data.get("default_length"),
        )
    except ValueError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_melody",
            translation_placeholders={"error": str(err)},
        ) from err
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
    hass.services.async_register(
        DOMAIN, "activate_led", _async_activate_led, schema=SCHEMA_ACTIVATE_LED
    )
    hass.services.async_register(
        DOMAIN, "activate_buzzer", _async_activate_buzzer, schema=SCHEMA_ACTIVATE_BUZZER
    )
    hass.services.async_register(
        DOMAIN, "write_nfc", _async_write_nfc, schema=SCHEMA_WRITE_NFC
    )
    hass.services.async_register(
        DOMAIN, "play_melody", _async_play_melody, schema=SCHEMA_PLAY_MELODY
    )
