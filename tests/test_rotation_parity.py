"""Preview-contract parity: the designer render endpoint's shape/orientation.

CORRECTION (tier-2 round 2, reviewer re-verification): this module
previously asserted only `endpoint bytes == send-path bytes` and treated
that as proof preview was correct. That wasn't a wrong thing to prove -- it
was the WRONG PROPERTY for what preview needs, and the suite was
structurally blind to the actual bug. The send path and the (then-buggy)
render endpoint both funneled through `prepare_image`'s DEVICE-FACING
`rotate` + fit-to-native-pixel-grid step, so they always agreed with EACH
OTHER while both landing on the same wrong shape for preview: the raw,
untransposed device grid, never the designer's own `context.display`
geometry (`HostPreviewDisplayGeometry`, vendored `.d.ts`: "the logical
drawing surface the payload is authored against ... never the raw physical
panel size, never a transform to apply"). For a base=0 display with a
90/270 orientation (the maintainer's real ESL 5 3.5", see the acceptance
vector below), NO `rotate` value made the OLD endpoint return the
transposed logical surface -- the designer letterboxed the wrong-shaped
answer into its own canvas: sideways content, despite the send path (which
correctly targets the native device grid) being fine all along.

Fixed in `designer/render.py`: the preview call to `prepare_image` now
passes an explicit `DeviceCapabilities` describing the LOGICAL surface
itself (width/height = the already-transposed `generate_image` canvas,
rotation=0) and `rotate=Rotation.ROTATE_0`, instead of the real device
capabilities plus the request's `rotate` value -- device-facing rotation
belongs only on the send path.

Three independent properties per (base, orientation) cell, none of them
"endpoint agrees with the buggy send-shaped value":

1. DIMENSIONS: endpoint output size == the logical surface size, from an
   independent formula (not imported from render.py).
2. CONTENT ORIENTATION: an asymmetric top-edge-bar payload lands on the TOP
   edge of the returned image, not some other edge -- catches a
   dimension-correct-but-rotated-the-wrong-way regression a dims-only check
   cannot.
3. PIPELINE PARITY: the endpoint's bytes match `prepare_image`'s own
   dither/quantize/palette output when called directly on the SEND path's
   own real `generate_image` image (captured via a `wraps=` spy, not
   reimplemented), with the logical surface as target and
   `rotate=ROTATE_0` -- proves preview shares the send path's real
   quantization pipeline without re-asserting the wrong device-grid-shaped
   property the old suite checked.
"""

from dataclasses import replace
import io
from unittest.mock import MagicMock, patch

from opendisplay import (
    ColorScheme,
    DeviceCapabilities,
    DitherMode,
    Rotation,
    prepare_image as real_prepare_image,
)
from PIL import Image as PILImage
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opendisplay.const import DOMAIN
from custom_components.opendisplay.designer.render import DESIGNER_RENDER_URL
from custom_components.opendisplay.services import (
    SCHEMA_DRAWCUSTOM,
    tone_and_measured_palettes_from_call_data,
)

from . import DEVICE_CONFIG

_SEND_PATH_TONE, _SEND_PATH_MEASURED_PALETTES = (
    tone_and_measured_palettes_from_call_data(SCHEMA_DRAWCUSTOM({"payload": []}))
)


def _expected_gen_dims(
    base_deg: int, rotate: int, pixel_width: int, pixel_height: int
) -> tuple[int, int]:
    """Return the logical surface's own dims, per services.py's contract.

    Deliberately re-derived here rather than imported from render.py/
    services.py: an independent ground truth, so a regression that breaks
    the transpose formula in the implementation doesn't also break the
    test's own expectation.
    """
    if (base_deg + rotate) % 360 in (90, 270):
        return pixel_height, pixel_width
    return pixel_width, pixel_height


def _top_bar_payload(width: int, height: int) -> list[dict]:
    """Return a payload with a black bar along the TOP edge, full width.

    Asymmetric on purpose: a bar anywhere but the intended top edge (a 180
    flip puts it on the bottom; a residual device-facing rotation moves it
    to a side edge or shrinks/stretches it) is trivially distinguishable by
    sampling the top and bottom rows.
    """
    bar_height = max(2, min(8, height // 4))
    return [
        {
            "type": "rectangle",
            "x_start": 0,
            "y_start": 0,
            "x_end": width,
            "y_end": bar_height,
            "fill": "black",
        }
    ]


@pytest.fixture
def mock_upload_device(mock_opendisplay_device: MagicMock) -> MagicMock:
    """Return the mock OpenDisplayDevice, for asserting upload_prepared_image args."""
    return mock_opendisplay_device


@pytest.fixture
def device_config(request: pytest.FixtureRequest):
    """Return DEVICE_CONFIG with displays[0] overridden per the case's param.

    `request.param` is either a bare int (rotation/base only -- the matrix
    test below, which otherwise keeps DEVICE_CONFIG's own 296x128/BWR
    display) or a dict of `DisplayConfig` field overrides (the ESL5
    acceptance vector, further down, which also overrides pixel dims and
    color_scheme to the maintainer's real reported attributes).
    """
    param = request.param
    overrides = param if isinstance(param, dict) else {"rotation": param}
    display = replace(DEVICE_CONFIG.displays[0], **overrides)
    return replace(DEVICE_CONFIG, displays=[display])


@pytest.fixture(autouse=True)
async def setup_entry(hass, mock_config_entry: MockConfigEntry) -> None:
    """Set up the config entry for parity tests."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def device_id(hass, mock_config_entry: MockConfigEntry) -> str:
    """Return the device registry ID for the fabricated config entry."""
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    assert devices
    return devices[0].id


async def _render_endpoint_image(
    hass_client, device_id: str, rotate: int, payload: list[dict]
) -> PILImage.Image:
    """POST to the render endpoint and return the decoded PNG as a PIL image."""
    client = await hass_client()
    resp = await client.post(
        DESIGNER_RENDER_URL,
        json={
            "device_id": device_id,
            "payload": payload,
            "background": "white",
            "dither": "none",
            "rotate": rotate,
        },
    )
    assert resp.status == 200, await resp.text()
    body = await resp.read()
    return PILImage.open(io.BytesIO(body)).convert("RGB")


async def _send_paths_pregenerated_image(
    hass, device_id: str, payload: list[dict], rotate: int
) -> PILImage.Image:
    """Run the real drawcustom send path and capture generate_image's own output.

    Spies on `prepare_image` (`wraps=real_prepare_image`, so the send path
    still runs for real) and returns the positional `img` argument it was
    called with -- the send path's own real `generate_image` output, BEFORE
    any device-facing rotate/fit. This is the shared upstream artifact both
    the send path and the render endpoint are supposed to build identically
    from; the "pre-rotation reference" in the pipeline-parity test below is
    built by feeding this same image through `prepare_image` a second time,
    independently, with the LOGICAL surface as target instead of the device
    grid.
    """
    with patch(
        "custom_components.opendisplay.services.prepare_image",
        wraps=real_prepare_image,
    ) as spy:
        await hass.services.async_call(
            DOMAIN,
            "drawcustom",
            {
                "device_id": [device_id],
                "payload": payload,
                "rotate": rotate,
                "dither": "none",
            },
            blocking=True,
            return_response=True,
        )
    return spy.call_args.args[0]


def _row_is_mostly_black(image: PILImage.Image, y: int) -> bool:
    row = [image.getpixel((x, y)) for x in range(image.width)]
    dark = sum(1 for px in row if sum(px[:3]) < 200)
    return dark > image.width * 0.8


def _row_is_mostly_white(image: PILImage.Image, y: int) -> bool:
    row = [image.getpixel((x, y)) for x in range(image.width)]
    light = sum(1 for px in row if sum(px[:3]) > 600)
    return light > image.width * 0.8


@pytest.mark.parametrize("device_config", [0, 90], indirect=True)
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
async def test_endpoint_returns_the_logical_surface_correctly_oriented(
    hass,
    device_id: str,
    hass_client,
    device_config,
    rotate: int,
) -> None:
    """Dimensions AND content orientation, for every (base, orientation) cell.

    Red-first (tier-2 round 2): fails on 4c8edf2 for every `rotate > 0`
    cell -- the old endpoint returned the native device grid (dims wrong
    whenever the transpose applies) with the bar rotated into the pipeline
    the send path uses for physical delivery, not the untouched logical
    surface preview needs.
    """
    display = device_config.displays[0]
    gen_width, gen_height = _expected_gen_dims(
        display.rotation, rotate, display.pixel_width, display.pixel_height
    )
    payload = _top_bar_payload(gen_width, gen_height)

    image = await _render_endpoint_image(hass_client, device_id, rotate, payload)

    assert image.size == (gen_width, gen_height), (
        f"base={display.rotation} rotate={rotate}: "
        f"endpoint {image.size} != logical surface {(gen_width, gen_height)}"
    )
    assert _row_is_mostly_black(image, 0), (
        f"base={display.rotation} rotate={rotate}: top edge is not the bar"
    )
    assert _row_is_mostly_white(image, image.height - 1), (
        f"base={display.rotation} rotate={rotate}: bottom edge is not clear "
        "-- bar landed on the wrong edge"
    )


@pytest.mark.parametrize("device_config", [0, 90], indirect=True)
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
async def test_endpoint_matches_send_paths_pre_rotation_pipeline(
    hass,
    device_id: str,
    hass_client,
    device_config,
    rotate: int,
) -> None:
    """Pipeline parity: same dither/quantize/palette, different final shape.

    Not "endpoint bytes == send-path bytes" (that was the wrong property --
    see the module docstring). Builds the reference by feeding the send
    path's own real generate_image output through prepare_image a second
    time, targeting the LOGICAL surface with rotate=ROTATE_0 -- exactly
    designer/render.py's own construction, but assembled independently here
    from the send path's real artifact rather than imported from render.py.
    """
    display = device_config.displays[0]
    gen_width, gen_height = _expected_gen_dims(
        display.rotation, rotate, display.pixel_width, display.pixel_height
    )
    payload = _top_bar_payload(gen_width, gen_height)

    pre_rotation_img = await _send_paths_pregenerated_image(
        hass, device_id, payload, rotate
    )
    color_scheme = ColorScheme.from_value(display.color_scheme)
    reference_capabilities = DeviceCapabilities(
        width=gen_width, height=gen_height, color_scheme=color_scheme
    )
    _, _, reference = await hass.async_add_executor_job(
        lambda: real_prepare_image(
            pre_rotation_img,
            capabilities=reference_capabilities,
            panel_ic_type=display.panel_ic_type,
            dither_mode=DitherMode.NONE,
            compress=False,
            tone=_SEND_PATH_TONE,
            use_measured_palettes=_SEND_PATH_MEASURED_PALETTES,
            rotate=Rotation.ROTATE_0,
        )
    )
    reference_rgb = reference.convert("RGB")

    endpoint_image = await _render_endpoint_image(
        hass_client, device_id, rotate, payload
    )

    assert endpoint_image.size == reference_rgb.size
    assert list(endpoint_image.getdata()) == list(reference_rgb.getdata())


_ESL5_ATTRS = {
    "pixel_width": 184,
    "pixel_height": 384,
    "rotation": 0,
    "color_scheme": 3,  # BWRY -- ColorScheme.BWRY.value
}


@pytest.mark.parametrize("device_config", [_ESL5_ATTRS], indirect=True)
async def test_esl5_3_5_real_hardware_acceptance_vector(
    hass,
    device_id: str,
    hass_client,
    device_config,
) -> None:
    """The maintainer's own real device, base=0, orientation=270.

    Canonical acceptance cell (tier-2 report, verbatim numbers): the ESL 5
    3.5"'s native pixel grid is 184x384 portrait, `rotation_degrees` is 0
    (no base persisted -- per-device persistence is the deferred upstream
    feature), color_scheme BWRY, physically mounted landscape -- the
    maintainer's own working automation compensates with `rotate: 270` on
    every `drawcustom` call. The PREVIEW endpoint must return the LOGICAL
    surface for that orientation: 384x184 landscape (NOT 184x384 -- that
    was the old, wrong assertion this test previously made, codifying the
    very bug this round fixes), with the top-edge bar landing on the
    returned image's top edge.
    """
    gen_width, gen_height = _expected_gen_dims(0, 270, 184, 384)
    assert (gen_width, gen_height) == (384, 184)
    payload = _top_bar_payload(gen_width, gen_height)

    image = await _render_endpoint_image(hass_client, device_id, 270, payload)

    assert image.size == (384, 184), image.size
    assert _row_is_mostly_black(image, 0)
    assert _row_is_mostly_white(image, image.height - 1)
