"""Preview-contract parity: the designer render endpoint's shape/orientation.

REVERSED (2026-08-31, maintainer report on real hardware): everything below
the tier-2 round 2 fix described a DELIBERATE divergence -- the render
endpoint targeted the LOGICAL surface (`rotate=Rotation.ROTATE_0`) so a
base=0 90/270 orientation returned the transposed canvas instead of the
native device grid. That made the shape bug (Bug 2, tier-2) go away, but it
also means 90 and 270 always returned pixel-identical previews -- the
maintainer flashed v2.9 and asked "didn't we want [orientation] to be also
correct so that 90 would look upside down in relation to 270?" It should:
the entity preview and a dry run already show the device-facing buffer
(tier-2 round 3, below), so the designer's own Display preview was left as
the ONE remaining view that could not catch a wrong orientation before
sending -- exactly backwards from what a preview is for.

THE RULING: the render endpoint's device_id path now calls the SAME
`_prepare_for_device` helper the send and dry-run paths use (`services.py`)
-- real device capabilities, `rotate` composed onto the device's own stored
base exactly as the send path derives it. Preview, dry run and a real send
now all produce the identical device-facing buffer for identical inputs; a
`renderPreview` request is a byte-for-byte rehearsal of what Send would
ship, not a separate rendering that merely resembles it.

Why this does not reintroduce Bug 2 (the tier-2 round 2 shape bug): Bug 2
was never in the delta FORMULA (`rotateDeltaFor`, still unchanged and still
correct -- `frontend/panel/rotation.js`); it was in feeding that correct
`rotate` through `prepare_image` while the two callers built the SOURCE
image at different shapes. Both the send path (`_drawcustom_for_device`)
and this endpoint build `generate_image`'s canvas at
`_expected_gen_dims(base, rotate, ...)` -- the transposed logical surface --
from the identical formula. `prepare_image` then rotates that source by
`(base + rotate) % 360` and fits to the native grid: by construction the
rotated source's dimensions already equal the native grid for every (base,
rotate) cell (proof: `_rotate_source_image` transposes width/height exactly
when the composed rotation is a quarter turn, which is exactly when
`_expected_gen_dims` itself swapped them to compensate), so `fit_image`
never has scaling/padding to do. `test_endpoint_matches_send_paths_prepared_buffer_pixel_for_pixel`
below proves this holds across the full (base, rotate) matrix, not just by
this argument.

The VIRTUAL display case (`display` spec, no `device_id`) is unchanged and
deliberately still targets the logical surface with `rotate=ROTATE_0`: it
has no HA device, so no stored base rotation exists to compose against --
`context.display` is already the final oriented surface the panel wrapper
sends `rotate: 0` for (`renderRequestBody`, `drawcustom-request.js`), and
device-facing vs. logical-surface is not even a distinct question when
there is no device.

Four properties per (base, orientation) cell now:

1. DIMENSIONS: endpoint output size == the device's own native pixel grid
   (`pixel_width`/`pixel_height`), invariant across every `rotate` value --
   NOT the logical surface size any more (that was this suite's own
   assertion through tier-2 round 2, and is now the wrong property).
2. CONTENT ORIENTATION: an asymmetric top-edge-bar payload lands on the
   edge implied by the COMPOSED rotation `(base + rotate) % 360`, not
   always the top -- 90 and 270 must land on opposite edges.
3. PIPELINE PARITY: the endpoint's decoded pixels match `prepare_image`'s
   own output when called directly on the SEND path's own real
   `generate_image` image (captured via a `wraps=` spy, not reimplemented),
   with the REAL device capabilities (derived from `config`, not a
   synthetic logical one) and the SAME `rotate` -- i.e. exactly what
   `_prepare_for_device` does.
4. SEND EQUALITY: the endpoint's response bytes, PNG-encoded, are
   byte-identical to `_encode_png` of the buffer a real (non-preview) send
   for the same device/payload/rotate hands to `upload_prepared_image` --
   the strongest form, mirroring the byte-equality already proven for dry
   runs below.

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

from opendisplay import DitherMode, Rotation, prepare_image as real_prepare_image
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


def _column_is_mostly_white(image: PILImage.Image, x: int) -> bool:
    col = [image.getpixel((x, y)) for y in range(image.height)]
    light = sum(1 for px in col if sum(px[:3]) > 600)
    return light > image.height * 0.8


# Direction convention, from the library itself (`opendisplay/device.py`):
# `prepare_image` composes `effective = (base + rotate) % 360` and rotates
# the source CLOCKWISE by it -- `Rotation.ROTATE_90 -> Image.Transpose.
# ROTATE_270` (PIL's ROTATE_270 is a 90-degree clockwise turn) and
# `Rotation.ROTATE_270 -> Image.Transpose.ROTATE_90` (90 counter-clockwise).
# So a top-edge bar in the source lands on: effective 0 -> top, 90 -> right,
# 180 -> bottom, 270 -> left. Keyed by the COMPOSED rotation, not the
# request's own `rotate` -- the two coincide only when base=0, which is why
# the base=0 characterisation tests further down index this dict directly
# by `rotate`.
_EDGE_BY_EFFECTIVE_ROTATION = {0: "top", 90: "right", 180: "bottom", 270: "left"}


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


@pytest.mark.parametrize("device_config", [0, 90], indirect=True)
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
async def test_endpoint_returns_the_device_facing_buffer(
    hass,
    device_id: str,
    hass_client,
    device_config,
    rotate: int,
) -> None:
    """Dimensions AND content orientation, for every (base, orientation) cell.

    REVERSED (2026-08-31): this used to assert the endpoint returns the
    LOGICAL surface, invariant of `rotate` -- see the module docstring for
    why that is now the wrong property. The payload is still authored
    against the logical surface (that is what the designer's canvas is at
    when it makes the request), but the returned PNG is now the
    DEVICE-FACING buffer: always the native pixel grid, with the bar on the
    edge implied by the composed rotation `(base + rotate) % 360`, not
    always the top.
    """
    display = device_config.displays[0]
    gen_width, gen_height = _expected_gen_dims(
        display.rotation, rotate, display.pixel_width, display.pixel_height
    )
    payload = _top_bar_payload(gen_width, gen_height)

    image = await _render_endpoint_image(hass_client, device_id, rotate, payload)

    native = (display.pixel_width, display.pixel_height)
    assert image.size == native, (
        f"base={display.rotation} rotate={rotate}: "
        f"endpoint {image.size} != native device grid {native}"
    )
    effective = (display.rotation + rotate) % 360
    expected_edge = _EDGE_BY_EFFECTIVE_ROTATION[effective]
    assert _bar_edge(image) == expected_edge, (
        f"base={display.rotation} rotate={rotate} (effective={effective}): "
        f"bar on the {_bar_edge(image)} edge, expected {expected_edge}"
    )


@pytest.mark.parametrize("device_config", [0, 90], indirect=True)
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
async def test_endpoint_matches_send_paths_prepared_buffer_pixel_for_pixel(
    hass,
    device_id: str,
    hass_client,
    device_config,
    rotate: int,
) -> None:
    """Pipeline parity: the endpoint now shares `_prepare_for_device`'s inputs.

    Builds the reference by feeding the send path's own real
    `generate_image` output through `prepare_image` a second time,
    targeting the REAL device capabilities (derived from `config`, no
    synthetic override) with the SAME `rotate` -- exactly what
    `_prepare_for_device` does, assembled independently here from the send
    path's real artifact rather than imported from render.py/services.py.

    Red-first (2026-08-31): fails against the pre-fix endpoint, which
    always returns the logical-surface size (previous module state) --
    `endpoint_image.size` would never equal `reference_rgb.size` for a cell
    where the transpose applies.
    """
    display = device_config.displays[0]
    gen_width, gen_height = _expected_gen_dims(
        display.rotation, rotate, display.pixel_width, display.pixel_height
    )
    payload = _top_bar_payload(gen_width, gen_height)

    pre_rotation_img = await _send_paths_pregenerated_image(
        hass, device_id, payload, rotate
    )
    _, _, reference = await hass.async_add_executor_job(
        lambda: real_prepare_image(
            pre_rotation_img,
            config=device_config,
            dither_mode=DitherMode.NONE,
            compress=False,
            tone=_SEND_PATH_TONE,
            use_measured_palettes=_SEND_PATH_MEASURED_PALETTES,
            rotate=Rotation(rotate),
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
    every `drawcustom` call.

    REVERSED (2026-08-31): the PREVIEW endpoint now returns the
    DEVICE-FACING buffer for that orientation -- 184x384 portrait (the
    panel's own native grid, NOT 384x184 -- that was this test's own
    assertion through the tier-2 round 2 fix, which is now the stale
    property; see the module docstring), with the top-edge bar landing on
    the LEFT edge (`effective = (0 + 270) % 360 = 270`,
    `_EDGE_BY_EFFECTIVE_ROTATION[270] == "left"`) -- matching the entity
    preview and a dry run for the identical call, which already showed this
    buffer (tier-2 round 3).
    """
    gen_width, gen_height = _expected_gen_dims(0, 270, 184, 384)
    assert (gen_width, gen_height) == (384, 184)
    payload = _top_bar_payload(gen_width, gen_height)

    image = await _render_endpoint_image(hass_client, device_id, 270, payload)

    assert image.size == (184, 384), image.size
    assert _bar_edge(image) == "left"


@pytest.mark.parametrize("device_config", [_ESL5_ATTRS], indirect=True)
async def test_preview_at_90_and_270_are_180_degrees_apart(
    hass,
    device_id: str,
    hass_client,
    device_config,
) -> None:
    """THE MAINTAINER'S REPORT, pinned directly against the preview endpoint.

    v2.9: "host rendering between 90 and 270 is still identical (both show
    up)". Same canvas, same payload, only the Orientation control differs
    between the two requests -- the two previews must now differ, and by
    exactly a half turn: rotating one 180 degrees must land pixel-for-pixel
    on the other. Red-first against the pre-fix endpoint, which returned the
    logical surface (identical for 90 and 270 by construction) for both.
    """
    payload = _top_bar_payload(384, 184)

    preview_90 = await _render_endpoint_image(hass_client, device_id, 90, payload)
    preview_270 = await _render_endpoint_image(hass_client, device_id, 270, payload)

    assert preview_90.size == preview_270.size == (184, 384)
    assert _bar_edge(preview_90) == "right"
    assert _bar_edge(preview_270) == "left"
    assert list(preview_90.transpose(PILImage.Transpose.ROTATE_180).getdata()) == list(
        preview_270.getdata()
    ), "orientation 90 and 270 previews must be 180 degrees apart, and nothing else"


@pytest.mark.parametrize("device_config", [_ESL5_ATTRS], indirect=True)
@pytest.mark.parametrize("rotate", [90, 270])
async def test_preview_matches_the_send_paths_prepared_buffer_byte_for_byte(
    hass,
    device_id: str,
    hass_client,
    device_config,
    mock_upload_device: MagicMock,
    rotate: int,
) -> None:
    """The strongest form: PNG-encoded preview bytes == PNG-encoded send buffer.

    Mirrors the byte-equality already proven for dry runs
    (`test_dry_run_and_the_real_send_publish_identical_previews`, below): a
    real (non-preview) send for the identical device/payload/rotate hands
    `upload_prepared_image` a processed image; `_encode_png` of that image
    must be byte-identical to the render endpoint's own response body for
    the same inputs -- not merely same-shaped or same-edge, but the exact
    same bytes, because both now go through the SAME `_prepare_for_device`
    call.
    """
    payload = _top_bar_payload(384, 184)

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
    sent_image = mock_upload_device.upload_prepared_image.call_args.args[0][2]
    sent_png = io.BytesIO()
    sent_image.save(sent_png, format="PNG")

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
    assert resp.status == 200
    preview_png = await resp.read()

    assert preview_png == sent_png.getvalue(), (
        "preview PNG bytes must match a real send's own prepared buffer, byte for byte"
    )


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
# top-edge bar lands on: 0 -> top, 90 -> right, 180 -> bottom, 270 -> left --
# `_EDGE_BY_EFFECTIVE_ROTATION` (defined earlier, alongside `_bar_edge`)
# already carries exactly this mapping; aliased here under the name these
# base=0 tests use ("rotate" and "effective" coincide only when base=0).
_TOP_BAR_EDGE_BY_ROTATE = _EDGE_BY_EFFECTIVE_ROTATION

_IMAGE_ENTITY = "image.opendisplay_1234_display_content"


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


# --- Dry run honesty (maintainer ruling: "dry run should be honest of course,
# otherwise it won't be a dry run") ------------------------------------------
#
# A dry run's entire purpose is to show what WOULD be sent without sending it.
# It used to publish the pre-rotation, un-dithered logical surface -- a
# different artifact from the one a real send publishes, and therefore a
# preview of something that would never reach any panel. Both halves of that
# are now the same call to `prepare_image` (`_prepare_for_device`), so a dry
# run and the real send it stands in for publish identical bytes.


async def _dry_run(hass, device_id: str, payload: list[dict], rotate: int, **extra):
    """Run a dry-run drawcustom and return its service response."""
    return await hass.services.async_call(
        DOMAIN,
        "drawcustom",
        {
            "device_id": [device_id],
            "payload": payload,
            "rotate": rotate,
            "dry-run": True,
            **extra,
        },
        blocking=True,
        return_response=True,
    )


async def _published_preview(hass, hass_client) -> PILImage.Image:
    """Return the image entity's current picture, as the dashboard shows it."""
    return PILImage.open(io.BytesIO(await _published_preview_bytes(hass, hass_client)))


async def _published_preview_bytes(hass, hass_client) -> bytes:
    state = hass.states.get(_IMAGE_ENTITY)
    assert state is not None
    client = await hass_client()
    resp = await client.get(state.attributes["entity_picture"])
    assert resp.status == 200
    return await resp.read()


@pytest.mark.parametrize("device_config", [_ESL5_ATTRS], indirect=True)
@pytest.mark.parametrize("rotate", [90, 270])
async def test_dry_run_previews_the_buffer_that_would_be_sent(
    hass,
    hass_client,
    device_id: str,
    device_config,
    mock_upload_device: MagicMock,
    rotate: int,
) -> None:
    """A dry run publishes the device-facing buffer, and uploads nothing.

    Red-first: the old dry-run branch published `_pil_to_jpeg(img)` -- the
    384x184 pre-rotation canvas, identical for 90 and 270 -- so this asserts
    both the 184x384 device grid and the per-orientation edge that the
    canvas cannot distinguish.
    """
    payload = _top_bar_payload(384, 184)

    response = await _dry_run(hass, device_id, payload, rotate)

    assert response["status"] == "dry_run"
    mock_upload_device.upload_prepared_image.assert_not_called()

    preview = (await _published_preview(hass, hass_client)).convert("RGB")
    assert preview.size == (184, 384), (
        "a dry run still previews the pre-rotation canvas -- it is showing "
        "something no panel would ever be sent"
    )
    assert _bar_edge(preview) == _TOP_BAR_EDGE_BY_ROTATE[rotate]


@pytest.mark.parametrize("device_config", [_ESL5_ATTRS], indirect=True)
async def test_dry_run_and_the_real_send_publish_identical_previews(
    hass,
    hass_client,
    device_id: str,
    device_config,
    mock_upload_device: MagicMock,
) -> None:
    """The honesty property itself: same call, same picture, one of them sent.

    Byte-for-byte, because both go through one `prepare_image` call with the
    same arguments and one `_pil_to_jpeg`. This is what makes the dry run a
    dry RUN rather than a different rendering that merely looks similar --
    and it transitively pins that a dry run honours `dither`, `rotate` and
    the tone/measured-palette derivation, since the real send provably does.
    """
    payload = _top_bar_payload(384, 184)

    await _dry_run(hass, device_id, payload, 270, dither="burkes")
    dry_run_preview = await _published_preview_bytes(hass, hass_client)
    mock_upload_device.upload_prepared_image.assert_not_called()

    await hass.services.async_call(
        DOMAIN,
        "drawcustom",
        {
            "device_id": [device_id],
            "payload": payload,
            "rotate": 270,
            "dither": "burkes",
        },
        blocking=True,
        return_response=True,
    )
    real_send_preview = await _published_preview_bytes(hass, hass_client)
    mock_upload_device.upload_prepared_image.assert_called_once()

    assert dry_run_preview == real_send_preview


@pytest.mark.parametrize("device_config", [_ESL5_ATTRS], indirect=True)
async def test_dry_run_honours_the_dither_mode(
    hass,
    hass_client,
    device_id: str,
    device_config,
    mock_upload_device: MagicMock,
) -> None:
    """A dry run dithers with the mode the call asked for.

    Closes a documented gap (`docs/designer.md`, "Known gaps"): the dry run
    always rendered the flat, un-dithered image, so on a BWRY panel a
    mid-tone looked like a mid-tone instead of the four-colour pattern the
    panel would actually show. A solid mid-grey is the discriminating
    payload -- `none` maps it to one flat palette entry, `burkes` diffuses
    the error across neighbouring pixels.
    """
    payload = [
        {
            "type": "rectangle",
            "x_start": 0,
            "y_start": 0,
            "x_end": 384,
            "y_end": 184,
            "fill": "#808080",
        }
    ]

    await _dry_run(hass, device_id, payload, 270, dither="none")
    flat = (await _published_preview(hass, hass_client)).convert("RGB")
    await _dry_run(hass, device_id, payload, 270, dither="burkes")
    dithered = (await _published_preview(hass, hass_client)).convert("RGB")

    assert flat.size == dithered.size == (184, 384)
    assert len(set(flat.getdata())) < len(set(dithered.getdata())), (
        "the two dither modes produced equally-flat output -- the dry run is "
        "ignoring `dither`"
    )
    mock_upload_device.upload_prepared_image.assert_not_called()
