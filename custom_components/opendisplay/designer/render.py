"""Synchronous render endpoint for the designer preview.

Maintainer ruling (2026-08-30): "designer play must never impact anything
around a live display." The designer's `renderPreview` host seam POSTs a
drawcustom-shaped payload here and gets back rendered PNG bytes directly --
no image-entity write, no SIGNAL_IMAGE_UPDATED dispatch, no BLE delivery, no
log line above debug.

It shares two calls with `_drawcustom_for_device`'s send path
(`generate_image`, then `prepare_image`'s dither/quantize step), with the
**same kwargs** the send path derives for a call carrying neither
``tone_compression`` nor ``measured_palette`` (`tone="auto"`,
`use_measured_palettes=False` -- `SCHEMA_DRAWCUSTOM`'s own defaults, not
`prepare_image`'s own defaults, which differ: passing neither kwarg at all
silently picks up `prepare_image(tone=0.0, use_measured_palettes=True)`
instead and renders a visibly different image on any panel with a measured
palette). It stops before the send-only tail: no upload, no queue, no
entity write.

This is the integration's first authenticated HTTP view.
"""

from __future__ import annotations

import functools
import io
import logging
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from odl_renderer import generate_image
from PIL import Image as PILImage
import voluptuous as vol

from custom_components.opendisplay.services import (
    SCHEMA_DRAWCUSTOM,
    HADataProvider,
    _dither_value,
    _font_search_dirs,
    _get_entry_for_device_id,
    render_payload_templates,
    tone_and_measured_palettes_from_call_data,
)
from opendisplay import ColorScheme, DeviceCapabilities, Rotation, prepare_image

_LOGGER = logging.getLogger(__name__)

DESIGNER_RENDER_URL = "/api/opendisplay/designer/render"

# The send path's own runtime values for a call that supplies neither
# tone_compression nor measured_palette, computed by validating an
# otherwise-empty call against the REAL SCHEMA_DRAWCUSTOM and reusing the
# send path's own derivation function -- not hardcoded independently of
# either, so a future change to the schema's defaults or the formula moves
# this too instead of the two silently drifting apart.
_SEND_PATH_DEFAULT_TONE, _SEND_PATH_DEFAULT_USE_MEASURED_PALETTES = (
    tone_and_measured_palettes_from_call_data(SCHEMA_DRAWCUSTOM({"payload": []}))
)

# A payload this large has no legitimate live-preview use (the whole point
# of `renderPreview` is a fast round-trip while editing) and `generate_image`
# runs on the event loop like the send path's own call does -- measured at
# ~0.22-0.25s worst case AT this cap (800x480, 1000 short text elements;
# a smaller canvas or simpler elements render considerably faster -- see
# docs/designer.md's "Known gaps" for the full measurement), which is long
# enough to matter for an endpoint that gets called on every debounced
# edit, not just once per deliberate send. This cap bounds that without
# touching `generate_image`'s call pattern itself (moving it into an
# executor would mean either handing a fresh, loop-bound
# `aiohttp.ClientSession` across threads -- session objects are not safe to
# use off the loop they were created on -- or forking `odl_renderer`'s own
# async/CPU-bound mix, neither of which this endpoint should do
# unilaterally; the send path shares the same characteristic today).
_MAX_ELEMENTS = 1000

# Virtual-display preview (tier-1 round 2, finding 2): the designer's
# "Virtual display" pick has no HA device behind it at all -- context.targetId
# is null, so there is no device_id this endpoint can resolve. The renderer
# itself never needed a device either, only geometry + a palette
# (generate_image/prepare_image both take width/height/color-scheme values,
# not a device object) -- so a request MAY supply an explicit `display` spec
# instead of `device_id`. `color_scheme` matches the SAME numeric vocabulary
# capabilities.py already publishes to the panel (`int(ColorScheme.value)`,
# not a string) and defaults to MONO: the designer's own renderPreview
# context (`HostPreviewContext.display`, docs/embedding.md) carries only
# width/height/rotation -- the designer keeps its own color-mode control
# entirely inside its chrome (ADR-018: no host UI for it), so this host has
# no way to know which one the user picked for Virtual and does not guess;
# MONO is a legible, deterministic default for a display with no real
# palette to be accurate to.
_DISPLAY_SPEC_SCHEMA = vol.Schema(
    {
        vol.Required("width"): vol.All(vol.Coerce(int), vol.Range(min=1, max=4096)),
        vol.Required("height"): vol.All(vol.Coerce(int), vol.Range(min=1, max=4096)),
        vol.Optional("color_scheme", default=ColorScheme.MONO.value): vol.All(
            vol.Coerce(int), vol.In([cs.value for cs in ColorScheme])
        ),
    },
    extra=vol.REMOVE_EXTRA,
)

# Mirrors SCHEMA_DRAWCUSTOM's own defaults for the fields it shares
# (background/dither/rotate) -- the render endpoint is a read-only sibling of
# that service, not a new set of conventions. AT LEAST one of device_id/
# display is required -- checked after schema validation (below), not
# expressible as a plain voluptuous shape without ExactSequence/Any
# contortions that would obscure the actual "need one of these" error. NOT
# "exactly one": nothing rejects a request carrying both. device_id wins
# silently when both are present (see post()'s own `if device_id: ... else:
# ...` branch) -- documented explicitly (docs/designer.md) rather than left
# as an unstated implementation detail, since the panel itself never
# constructs a request with both (renderPreview sends exactly one, gated on
# whether context.targetId is null), so this precedence has no live caller
# to exercise it, only a hypothetical direct API caller.
_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): str,
        vol.Optional("display"): _DISPLAY_SPEC_SCHEMA,
        vol.Required("payload"): vol.All(list, vol.Length(max=_MAX_ELEMENTS)),
        vol.Optional("background", default="white"): str,
        vol.Optional("dither", default="burkes"): _dither_value,
        vol.Optional("rotate", default=0): vol.All(
            vol.Coerce(int), vol.In([0, 90, 180, 270])
        ),
    },
    extra=vol.REMOVE_EXTRA,
)


def _synthetic_global_config(width: int, height: int, color_scheme: int):
    """Build a syntactically real GlobalConfig for a device-less preview.

    Only `displays[0]`'s geometry/color_scheme actually matter to
    generate_image/prepare_image; the rest (system/manufacturer/power, pin
    assignments) are wiring details a pure render never reads, filled with
    the same harmless placeholder values dev/inject-displays.py already uses
    to fabricate a syntactically valid device with no real hardware behind
    it -- see that script's own `_build_device_configs` for why these
    specific placeholders (0xFF unassigned-pin sentinels, zeroed reserved
    bytes) are safe.
    """
    from opendisplay import (
        BoardManufacturer,
        DisplayConfig,
        GlobalConfig,
        ManufacturerData,
        PowerOption,
        SystemConfig,
    )
    from opendisplay.models.enums import PowerMode

    system = SystemConfig(
        ic_type=0,
        communication_modes=0,
        device_flags=0,
        pwr_pin=0xFF,
        reserved=b"\x00" * 15,
    )
    power = PowerOption(
        power_mode=PowerMode.BATTERY,
        battery_capacity_mah=(2000).to_bytes(3, "little"),
        sleep_timeout_ms=10_000,
        tx_power=0,
        sleep_flags=0,
        battery_sense_pin=0xFF,
        battery_sense_enable_pin=0xFF,
        battery_sense_flags=0,
        capacity_estimator=0,
        voltage_scaling_factor=0,
        deep_sleep_current_ua=0,
        deep_sleep_time_seconds=0,
        charge_enable_pin=0xFF,
        charge_state_pin=0xFF,
        charger_flags=0,
        min_wake_time_seconds=0,
        screen_timeout_seconds=0,
        reserved=b"\x00" * 4,
    )
    display = DisplayConfig(
        instance_number=0,
        display_technology=0,
        panel_ic_type=0,
        pixel_width=width,
        pixel_height=height,
        active_width_mm=0,
        active_height_mm=0,
        tag_type=0,
        rotation=0,
        reset_pin=0xFF,
        busy_pin=0xFF,
        dc_pin=0xFF,
        cs_pin=0xFF,
        data_pin=0,
        partial_update_support=0,
        color_scheme=color_scheme,
        transmission_modes=0x01,
        clk_pin=0,
        reserved_pins=b"\x00" * 7,
        full_update_mC=0,
        reserved=b"\x00" * 13,
    )
    return GlobalConfig(
        system=system,
        manufacturer=ManufacturerData(
            manufacturer_id=BoardManufacturer.SEEED,
            board_type=0,
            board_revision=0,
            reserved=b"\x00" * 6,
        ),
        power=power,
        displays=[display],
    )


def _encode_png(img: PILImage.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class OpenDisplayDesignerRenderView(HomeAssistantView):
    """POST a drawcustom-shaped payload for one device; get back a PNG.

    Never touches the target's image entity, never dispatches
    SIGNAL_IMAGE_UPDATED, never queues or delivers to the device -- a
    designer preview must be inert with respect to everything a real send
    would change.
    """

    url = DESIGNER_RENDER_URL
    name = "api:opendisplay:designer:render"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Render a drawcustom payload for one device and return PNG bytes."""
        hass = self.hass
        try:
            body: Any = await request.json()
        except ValueError:
            return web.json_response({"message": "invalid JSON body"}, status=400)

        try:
            data = _SCHEMA(body)
        except vol.Invalid as err:
            return web.json_response(
                {"message": f"invalid render request: {err}"}, status=400
            )

        device_id: str | None = data.get("device_id")
        display_spec: dict[str, int] | None = data.get("display")
        if not device_id and not display_spec:
            return web.json_response(
                {"message": "either device_id or display (width/height) is required"},
                status=400,
            )

        log_target: str
        if device_id:
            try:
                entry = _get_entry_for_device_id(hass, device_id)
            except ServiceValidationError:
                return web.json_response(
                    {"message": f"unknown device_id: {device_id}"}, status=404
                )

            displays = entry.runtime_data.device_config.displays
            if not displays:
                return web.json_response(
                    {"message": f"device {device_id} has no display configured"},
                    status=400,
                )
            display = displays[0]
            config = entry.runtime_data.device_config
            log_target = f"device={device_id}"
        else:
            # Virtual-display preview (tier-1 round 2, finding 2): no HA
            # device exists to resolve at all -- see _synthetic_global_config
            # and _DISPLAY_SPEC_SCHEMA's own doc comments for why a
            # syntactically real, but device-less, GlobalConfig is enough.
            assert display_spec is not None  # narrowed by the check above
            config = _synthetic_global_config(
                display_spec["width"],
                display_spec["height"],
                display_spec["color_scheme"],
            )
            display = config.displays[0]
            log_target = f"display={display_spec['width']}x{display_spec['height']}"
        cs = display.color_scheme_enum
        color_scheme = cs if isinstance(cs, ColorScheme) else ColorScheme.from_value(cs)

        rotate: int = data["rotate"]
        # Same transpose rule as _drawcustom_for_device: the payload is
        # authored against the final on-screen orientation, so when the
        # effective rotation swaps the axes the canvas is generated
        # transposed too.
        base = display.rotation_enum
        base_deg = base.value if isinstance(base, Rotation) else 0
        if (base_deg + rotate) % 360 in (90, 270):
            gen_width, gen_height = display.pixel_height, display.pixel_width
        else:
            gen_width, gen_height = display.pixel_width, display.pixel_height

        # Same template expansion the send path applies (tier-1 adversarial
        # review, finding 1): a literal '{{ ... }}' must not reach
        # odl-renderer unevaluated. Rendered ahead of, and reported
        # separately from, the generate_image try/except below so a broken
        # template gets its own specific "which element, why" message
        # rather than the generic "invalid payload" one that block reports.
        try:
            rendered_payload = render_payload_templates(hass, data["payload"])
        except ServiceValidationError as err:
            return web.json_response({"message": str(err)}, status=400)

        try:
            img = await generate_image(
                width=gen_width,
                height=gen_height,
                elements=rendered_payload,
                background=data["background"],
                accent_color=color_scheme.accent_color,
                session=async_get_clientsession(hass),
                data_provider=HADataProvider(hass),
                font_dirs=await hass.async_add_executor_job(_font_search_dirs, hass),
            )
        except Exception as err:
            # The exception text can carry local filesystem detail (a font
            # search path in a "file not found" message) -- worth having in
            # the debug log, not worth handing back to whatever called this
            # endpoint.
            _LOGGER.debug("designer render failed for %s: %s", log_target, err)
            return web.json_response(
                {"message": "render failed: invalid payload"}, status=400
            )

        # Same dither + quantize pipeline the send path uses
        # (_async_send_image's prepare_image call), minus the compressed
        # device-upload payload (compress=False) and any upload/queue/entity
        # side effect -- this stops at the dithered PIL image and never
        # calls anything past it. `tone`/`use_measured_palettes` are the
        # send path's own derived values for an unset tone_compression/
        # measured_palette (see the module docstring) -- passing neither
        # kwarg here would silently fall back to `prepare_image`'s own
        # different defaults instead.
        #
        # CRITICAL divergence from the send path (reviewer-reproduced,
        # tier-2 round 2): prepare_image's OWN target_size is always the
        # raw, un-transposed device pixel grid (`capabilities.width/height`,
        # from `config` -- correct for the send path, which uploads to that
        # physical buffer), and its `rotate` is DEVICE-FACING: it composes
        # with the device's own base rotation and re-fits to that native
        # grid regardless of what rotate value is passed. Passing the
        # request's `rotate` here (as an earlier version of this endpoint
        # did) meant preview output was ALWAYS shaped to the native device
        # grid, never to the transposed `(gen_width, gen_height)` canvas
        # already built above -- for a base=0 display with a 90/270
        # orientation, no `rotate` value could make the response land at
        # the designer's own `context.display` geometry (`HostPreviewDisplayGeometry`,
        # vendored `.d.ts`: "the logical drawing surface the payload is
        # authored against ... never the raw physical panel size, never a
        # transform to apply"). The designer then letterboxed a
        # wrong-shaped answer into its own canvas -- sideways content,
        # despite a correct canvas on the CLIENT side.
        #
        # The fix: build an explicit DeviceCapabilities describing the
        # LOGICAL surface itself (width/height = gen_width/gen_height,
        # rotation=0) instead of letting prepare_image derive one from
        # `config` (the real, raw device grid) -- so its target_size
        # already equals what generate_image just produced (no fit_image
        # distortion) and rotate=ROTATE_0 no-ops (no extra device-facing
        # spin). `config` is still passed for palette/panel_ic_type
        # derivation (color_scheme/panel_ic_type come from the real
        # display, not the synthetic capabilities). Device-facing rotation
        # belongs ONLY on the send path (`_drawcustom_for_device` /
        # `_async_send_image`), which this preview-only object deliberately
        # never touches.
        preview_capabilities = DeviceCapabilities(
            width=gen_width,
            height=gen_height,
            color_scheme=color_scheme,
        )
        _, _, dithered = await hass.async_add_executor_job(
            functools.partial(
                prepare_image,
                img,
                config=config,
                capabilities=preview_capabilities,
                dither_mode=data["dither"],
                compress=False,
                tone=_SEND_PATH_DEFAULT_TONE,
                use_measured_palettes=_SEND_PATH_DEFAULT_USE_MEASURED_PALETTES,
                rotate=Rotation.ROTATE_0,
            )
        )
        png_bytes = await hass.async_add_executor_job(_encode_png, dithered)
        _LOGGER.debug(
            "designer render endpoint: %s dither=%s bytes=%d",
            log_target,
            data["dither"],
            len(png_bytes),
        )
        return web.Response(body=png_bytes, content_type="image/png")
