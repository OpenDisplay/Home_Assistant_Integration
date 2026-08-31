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
geometry (`HostDisplayGeometry`, vendored `.d.ts`: "the logical
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

SEND WITHOUT PREVIEW (designer 3.0.0, issue #105 WYSIWYG-send slice): the
panel used to reuse the last preview render's `rotate` at Send time, so a
Send with no preview ever run shipped `rotate: 0` -- sideways content on a
rotated display, on real hardware. `HostActionContext` now carries the live
`display.rotation`, the panel derives `rotate` from it at click time
(`tests/js/drawcustom-request.test.mjs` pins the JS half), and
`test_send_without_preview_carries_the_rotate_into_the_buffer` below
pins the Python half:
a `drawcustom` call carrying that derived rotate, and NOTHING else -- no
preview request before it -- hands the device a native-grid buffer with the
content actually turned. It asserts on the image handed to
`upload_prepared_image`, which is what physically reaches the panel.

THE MAPPING both halves implement, in one sentence: the designer reports an
ABSOLUTE on-screen orientation (`context.display.rotation`) while `rotate`
-- on this endpoint and on `opendisplay.drawcustom` alike -- is a DELTA the
device composes onto its own stored base rotation, so the panel converts
with `rotate = (context.display.rotation - target.display.rotationDegrees)
mod 360`.
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


def _column_is_mostly_black(image: PILImage.Image, x: int) -> bool:
    col = [image.getpixel((x, y)) for y in range(image.height)]
    dark = sum(1 for px in col if sum(px[:3]) < 200)
    return dark > image.height * 0.8


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


@pytest.mark.parametrize("device_config", [_ESL5_ATTRS], indirect=True)
async def test_send_without_preview_carries_the_rotate_into_the_buffer(
    hass,
    device_id: str,
    device_config,
    mock_upload_device: MagicMock,
) -> None:
    """A `drawcustom` send carrying the panel's derived rotate, and no preview.

    RENAMED (tier-2 round 3). This test was called
    `test_send_without_preview_lands_right_side_up`, and its assertions
    were right for a reason it could not substantiate: "right side up" is a
    fact about the PHYSICAL MOUNTING, which no code here knows. What it
    actually proves -- and all it can prove -- is that the `rotate` reaches
    the device buffer and turns the content a quarter turn. Whether that
    quarter turn matches a given wall is the user's Orientation choice, and
    only one of an opposite pair ever can (see the characterisation block
    below).

    The WYSIWYG-send path (designer 3.0.0, issue #105): the panel derives
    `rotate` from `context.display.rotation` at click time, so this call is
    the FIRST and ONLY render of the session -- no preview request runs
    before it, and nothing is remembered from one. This test deliberately
    never touches the render endpoint, which is exactly the case the old
    sticky `_lastPreviewRotate` got wrong (it shipped `rotate: 0`).

    Asserts on what physically reaches the panel: the processed image inside
    the `prepared` tuple handed to `upload_prepared_image`. That is the
    device-facing buffer -- the raw native 184x384 grid, NOT the logical
    surface preview returns -- with the payload's top-edge bar turned onto
    the LEFT edge, because `prepare_image` rotates the source by
    (base + rotate) = 270 with CLOCKWISE semantics
    (`Rotation.ROTATE_270` -> `Image.Transpose.ROTATE_90`, i.e. a quarter
    turn counter-clockwise in PIL's own frame), which carries a top row onto
    the left column.

    Red-first: with `rotate: 0` (what a Send without a preview used to ship)
    the bar stays on the TOP edge of a 184x384 buffer, failing both the
    left-edge and the not-on-top assertions below.
    """
    # Authored against the LOGICAL surface for orientation 270, exactly what
    # the designer's canvas is at when the user clicks Send.
    payload = _top_bar_payload(384, 184)

    await hass.services.async_call(
        DOMAIN,
        "drawcustom",
        {
            "device_id": [device_id],
            "payload": payload,
            "rotate": 270,
            "dither": "none",
        },
        blocking=True,
        return_response=True,
    )

    prepared = mock_upload_device.upload_prepared_image.call_args.args[0]
    uploaded = prepared[2].convert("RGB")

    assert uploaded.size == (184, 384), (
        f"send must upload the native device grid, got {uploaded.size}"
    )
    assert _column_is_mostly_black(uploaded, 0), (
        "the bar did not land on the left edge -- the rotate never reached "
        "the device buffer"
    )
    assert _row_is_mostly_white(uploaded, 0), (
        "the bar is still on the top edge -- the payload shipped un-rotated"
    )


# --- Orientation characterisation (tier-2 round 3) --------------------------
#
# WHAT THE MAINTAINER SAW, on his own wall: designing a landscape canvas for
# an ESL 5 3.5" (native 184x384 portrait, `rotation_degrees` 0, physically
# mounted landscape), Orientation 270 came out upright and Orientation 90
# came out upside down -- while the designer canvas AND the entity's stored
# preview looked correct in BOTH cases.
#
# THAT IS NOT A SIGN BUG, and the tests below exist to pin exactly why, so
# that a future change to the mapping is visible rather than silent:
#
#   * the LOGICAL SURFACE (`generate_image`'s canvas) is IDENTICAL for 90
#     and 270 -- same transposed dimensions, same payload, same pixels;
#   * the DEVICE BUFFER (what `upload_prepared_image` receives, and what the
#     panel physically paints) differs by exactly 180 degrees between them.
#
# A panel has ONE physical mounting, so for that mounting exactly one of
# {90, 270} yields an upright wall image and the other is necessarily 180
# degrees out. Flipping the sign of the mapping would only move the
# upside-down case from 90 to 270. These are CHARACTERISATION tests: they
# assert what the code does today, in the artifact that physically reaches
# the panel, and they deliberately make no claim about which way up any
# particular wall is -- that depends on the mounting, which no code here
# can know.
#
# The direction convention, from the library itself
# (`opendisplay/device.py`): `prepare_image` composes
# `effective = (base + rotate) % 360` and rotates the source CLOCKWISE by
# it -- `Rotation.ROTATE_90 -> Image.Transpose.ROTATE_270` (PIL's ROTATE_270
# is a 90-degree clockwise turn) and `Rotation.ROTATE_270 ->
# Image.Transpose.ROTATE_90` (90 counter-clockwise). So for base 0 a
# top-edge bar lands on: 0 -> top, 90 -> right, 180 -> bottom, 270 -> left.

_TOP_BAR_EDGE_BY_ROTATE = {0: "top", 90: "right", 180: "bottom", 270: "left"}

_IMAGE_ENTITY = "image.opendisplay_1234_display_content"


def _column_is_mostly_white(image: PILImage.Image, x: int) -> bool:
    col = [image.getpixel((x, y)) for y in range(image.height)]
    light = sum(1 for px in col if sum(px[:3]) > 600)
    return light > image.height * 0.8


def _bar_edge(image: PILImage.Image) -> str:
    """Return which single edge of `image` the black bar occupies."""
    edges = {
        "top": _row_is_mostly_black(image, 0),
        "bottom": _row_is_mostly_black(image, image.height - 1),
        "left": _column_is_mostly_black(image, 0),
        "right": _column_is_mostly_black(image, image.width - 1),
    }
    found = [name for name, hit in edges.items() if hit]
    assert len(found) == 1, f"expected the bar on exactly one edge, got {found}"
    return found[0]


async def _sent_device_buffer(
    hass, device_id: str, payload: list[dict], rotate: int, upload_device: MagicMock
) -> PILImage.Image:
    """Run a real send and return the buffer handed to upload_prepared_image."""
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
    prepared = upload_device.upload_prepared_image.call_args.args[0]
    return prepared[2].convert("RGB")


@pytest.mark.parametrize("device_config", [_ESL5_ATTRS], indirect=True)
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
async def test_device_buffer_orientation_characterisation(
    hass,
    device_id: str,
    device_config,
    mock_upload_device: MagicMock,
    rotate: int,
) -> None:
    """CHARACTERISATION: where a top-edge bar lands in the uploaded buffer.

    One cell per orientation, on the maintainer's own device (base 0,
    native 184x384). Documents current behavior in the artifact that
    physically reaches the panel; changing the mapping must change these.
    """
    gen_width, gen_height = _expected_gen_dims(0, rotate, 184, 384)
    payload = _top_bar_payload(gen_width, gen_height)

    uploaded = await _sent_device_buffer(
        hass, device_id, payload, rotate, mock_upload_device
    )

    assert uploaded.size == (184, 384), (
        f"rotate={rotate}: the upload must always be the native device grid"
    )
    assert _bar_edge(uploaded) == _TOP_BAR_EDGE_BY_ROTATE[rotate], (
        f"rotate={rotate}: bar on the {_bar_edge(uploaded)} edge, "
        f"expected {_TOP_BAR_EDGE_BY_ROTATE[rotate]}"
    )


@pytest.mark.parametrize("device_config", [_ESL5_ATTRS], indirect=True)
async def test_ninety_and_two_seventy_share_a_canvas_and_differ_only_on_the_panel(
    hass,
    device_id: str,
    device_config,
    mock_upload_device: MagicMock,
) -> None:
    """CHARACTERISATION: the maintainer's exact observation, in two artifacts.

    Same payload at Orientation 90 and at Orientation 270:

      * the logical surface `generate_image` builds is pixel-identical --
        which is why the designer's canvas and the entity's preview looked
        correct in BOTH cases;
      * the buffer the panel is given is the 180-degree rotation of the
        other -- which is why exactly one of them was upright on the wall.

    Not a sign bug: a physical panel has one mounting, so one of any
    opposite pair is necessarily upside down. Flipping the mapping's sign
    would swap WHICH one, not remove the case.
    """
    payload = _top_bar_payload(384, 184)

    surface_90 = await _send_paths_pregenerated_image(hass, device_id, payload, 90)
    buffer_90 = mock_upload_device.upload_prepared_image.call_args.args[0][2].convert(
        "RGB"
    )
    surface_270 = await _send_paths_pregenerated_image(hass, device_id, payload, 270)
    buffer_270 = mock_upload_device.upload_prepared_image.call_args.args[0][2].convert(
        "RGB"
    )

    assert surface_90.size == surface_270.size == (384, 184)
    assert list(surface_90.convert("RGB").getdata()) == list(
        surface_270.convert("RGB").getdata()
    ), "the logical surface must not depend on which way the panel is turned"

    assert buffer_90.size == buffer_270.size == (184, 384)
    assert _bar_edge(buffer_90) == "right"
    assert _bar_edge(buffer_270) == "left"
    assert list(buffer_90.transpose(PILImage.Transpose.ROTATE_180).getdata()) == list(
        buffer_270.getdata()
    ), "the two buffers must be 180 degrees apart, and nothing else"


@pytest.mark.parametrize("device_config", [_ESL5_ATTRS], indirect=True)
@pytest.mark.parametrize("rotate", [90, 270])
async def test_the_entitys_preview_is_the_buffer_that_was_sent(
    hass,
    hass_client,
    device_id: str,
    device_config,
    mock_upload_device: MagicMock,
    rotate: int,
) -> None:
    """The image entity must show the POST-rotation buffer, not the canvas.

    THE BUG (tier-2 round 3, reproduced by the maintainer on real hardware
    WITHOUT the designer -- a plain `opendisplay.drawcustom` call with
    `rotate: 90` and then `rotate: 270`): at 90 the wall was upside down
    while the Home Assistant preview was upright; at 270 both were upright.
    The preview was built from the PRE-rotation logical surface, which is
    identical for two opposite orientations, so it could not distinguish
    them and a wrong choice was only discoverable by walking to the
    display. It is now built from the buffer handed to
    `upload_prepared_image`.

    This test calls the SERVICE directly -- no designer, no render
    endpoint, no panel -- because the bug is in the service, not in the
    designer: every `drawcustom`/`upload_image` caller was affected. The
    maintainer's own words: "the image preview in HA on the element should
    reflect what the display has."

    Both halves of the maintainer's pair are exercised, and they must now
    DIFFER: 90 and 270 put the bar on opposite edges of the same-shaped
    buffer, so a preview that still ignored rotation would give the same
    answer for both and fail one of them.

    Shape alone already discriminates the surface from the buffer (384x184
    landscape vs 184x384 portrait) and is robust to JPEG's lossiness; the
    bar edge is asserted too, so a right-shaped-but-wrong-way-round
    artifact cannot pass.

    NOT the designer's own preview endpoint (`designer/render.py`), which
    deliberately returns the LOGICAL surface for the designer's canvas and
    never touches this entity -- a different artifact, unchanged here.
    """
    payload = _top_bar_payload(384, 184)

    uploaded = await _sent_device_buffer(
        hass, device_id, payload, rotate, mock_upload_device
    )
    assert uploaded.size == (184, 384)
    expected_edge = _TOP_BAR_EDGE_BY_ROTATE[rotate]
    assert _bar_edge(uploaded) == expected_edge

    state = hass.states.get(_IMAGE_ENTITY)
    assert state is not None
    client = await hass_client()
    resp = await client.get(state.attributes["entity_picture"])
    assert resp.status == 200
    preview = PILImage.open(io.BytesIO(await resp.read())).convert("RGB")

    assert preview.size == uploaded.size, (
        "the entity preview is still the pre-rotation logical surface -- a "
        "wrong orientation stays invisible in Home Assistant"
    )
    assert _bar_edge(preview) == expected_edge, (
        "the entity preview is not turned the way the uploaded buffer is"
    )
