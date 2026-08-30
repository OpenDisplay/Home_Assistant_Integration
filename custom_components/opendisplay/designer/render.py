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
from opendisplay import ColorScheme, Rotation, prepare_image

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

# Mirrors SCHEMA_DRAWCUSTOM's own defaults for the fields it shares
# (background/dither/rotate) -- the render endpoint is a read-only sibling of
# that service, not a new set of conventions.
_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): str,
        vol.Required("payload"): vol.All(list, vol.Length(max=_MAX_ELEMENTS)),
        vol.Optional("background", default="white"): str,
        vol.Optional("dither", default="burkes"): _dither_value,
        vol.Optional("rotate", default=0): vol.All(
            vol.Coerce(int), vol.In([0, 90, 180, 270])
        ),
    },
    extra=vol.REMOVE_EXTRA,
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

        device_id: str = data["device_id"]
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
            _LOGGER.debug("designer render failed for device %s: %s", device_id, err)
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
        _, _, dithered = await hass.async_add_executor_job(
            functools.partial(
                prepare_image,
                img,
                config=entry.runtime_data.device_config,
                dither_mode=data["dither"],
                compress=False,
                tone=_SEND_PATH_DEFAULT_TONE,
                use_measured_palettes=_SEND_PATH_DEFAULT_USE_MEASURED_PALETTES,
                rotate=Rotation(rotate),
            )
        )
        png_bytes = await hass.async_add_executor_job(_encode_png, dithered)
        _LOGGER.debug(
            "designer render endpoint: device=%s dither=%s bytes=%d",
            device_id,
            data["dither"],
            len(png_bytes),
        )
        return web.Response(body=png_bytes, content_type="image/png")
