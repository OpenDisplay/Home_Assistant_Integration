"""Table-driven parity: the designer render endpoint vs. the drawcustom send path.

Tier-2 (real hardware) finding: a maintainer report that a rotated display's
SERVER preview rendered sideways despite a correct CLIENT canvas. His actual
device (the ESL 5 3.5", `test_esl5_3_5_real_hardware_acceptance_vector`
below) turned out to have `rotation_degrees` (base) = 0 -- native portrait
184x384, physically mounted landscape, base never persisted (per-device
persistence is a deferred upstream feature), his own working automation
compensating with `rotate: 270` on every call. `_drawcustom_for_device`
(services.py) and the render endpoint (`designer/render.py`) share the
identical `(base_deg + rotate) % 360 in (90, 270)` transpose rule and pass
the same `rotate=Rotation(rotate)` into `prepare_image`; this module proves
that sharing actually holds bytes-for-bytes for every `(base, rotate)`
combination in `{0, 90} x {0, 90, 180, 270}` -- including base=0, the
maintainer's own real case -- rather than trusting the two call sites stayed
in sync by inspection. See `docs/designer.md`'s tier-2 root-cause note for
the fuller writeup of what this investigation did and did not find.

For each cell: run the SAME payload through the drawcustom service (real
`generate_image`, no mocking) and capture what `prepare_image` actually
handed `upload_prepared_image` (the dithered PIL image, pre-encode); render
the SAME device_id/payload/rotate through `POST /api/opendisplay/designer/render`
and decode its PNG response. Both must agree on dimensions AND on pixel
content -- an asymmetric payload (a block in one corner) makes a
transpose/rotation mismatch show up as a pixel difference, not just a
same-looking swap.
"""

from dataclasses import replace
import io
from unittest.mock import MagicMock

from PIL import Image as PILImage
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opendisplay.const import DOMAIN
from custom_components.opendisplay.designer.render import DESIGNER_RENDER_URL

from . import DEVICE_CONFIG


@pytest.fixture
def mock_upload_device(mock_opendisplay_device: MagicMock) -> MagicMock:
    """Return the mock OpenDisplayDevice, for asserting upload_prepared_image args."""
    return mock_opendisplay_device


# An asymmetric payload: a solid black block against the top-left corner only.
# Any transpose/rotation mismatch between the two paths moves this block to a
# different corner (or leaves it stretched to the wrong aspect), which a
# pixel-for-pixel comparison catches even when width/height happen to match.
_PAYLOAD = [
    {
        "type": "rectangle",
        "x_start": 0,
        "y_start": 0,
        "x_end": 12,
        "y_end": 6,
        "fill": "black",
    }
]


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


async def _send_path_image(hass, device_id: str, mock_upload_device, rotate: int):
    """Run the real drawcustom send path and return the prepared PIL image."""
    await hass.services.async_call(
        DOMAIN,
        "drawcustom",
        {
            "device_id": [device_id],
            "payload": _PAYLOAD,
            "rotate": rotate,
            "dither": "none",
        },
        blocking=True,
        return_response=True,
    )
    prepared = mock_upload_device.upload_prepared_image.call_args.args[0]
    # `dithered` (index 2) is in the palette/quantized mode dither_image
    # produces (small integer pixel values, not RGB) -- convert through the
    # image's own palette so a color comparison against the PNG-decoded
    # endpoint image (below) is apples-to-apples rather than index-vs-RGB.
    return prepared[2].convert("RGB")


async def _render_endpoint_image(hass_client, device_id: str, rotate: int):
    """POST to the render endpoint and return the decoded PNG as a PIL image."""
    client = await hass_client()
    resp = await client.post(
        DESIGNER_RENDER_URL,
        json={
            "device_id": device_id,
            "payload": _PAYLOAD,
            "background": "white",
            "dither": "none",
            "rotate": rotate,
        },
    )
    assert resp.status == 200, await resp.text()
    body = await resp.read()
    return PILImage.open(io.BytesIO(body)).convert("RGB")


@pytest.mark.parametrize("device_config", [0, 90], indirect=True)
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
async def test_endpoint_matches_send_path_bytes(
    hass,
    device_id: str,
    mock_upload_device,
    hass_client,
    device_config,
    rotate: int,
) -> None:
    """The render endpoint must replicate the send path exactly.

    Same (base, rotate) cell -> same output dimensions and same pixels,
    whether the bytes came from the drawcustom service or from the designer's
    preview endpoint.
    """
    send_image = await _send_path_image(hass, device_id, mock_upload_device, rotate)
    endpoint_image = await _render_endpoint_image(hass_client, device_id, rotate)

    assert endpoint_image.size == send_image.size, (
        f"base={device_config.displays[0].rotation} rotate={rotate}: "
        f"endpoint {endpoint_image.size} != send-path {send_image.size}"
    )
    assert list(endpoint_image.getdata()) == list(send_image.getdata()), (
        f"base={device_config.displays[0].rotation} rotate={rotate}: pixels differ"
    )


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
    mock_upload_device,
    hass_client,
    device_config,
) -> None:
    """The maintainer's own real device, base=0, orientation=270.

    Canonical acceptance cell (tier-2 report, verbatim numbers): the ESL 5
    3.5"'s native pixel grid is 184x384 portrait, `rotation_degrees` is 0
    (no base persisted -- per-device persistence is the deferred upstream
    feature), color_scheme BWRY, and the panel is physically mounted
    landscape -- the maintainer's own working automation compensates with
    `rotate: 270` on every `drawcustom` call. Asserts the render endpoint
    matches the send path bytes-for-bytes at this EXACT real vector (not
    just the synthetic 296x128 matrix above) AND that the final output
    lands 184x384 (portrait, matching the device's native pixel grid -- the
    device applies no further rotation of its own beyond what `rotate`
    already accounts for) with content rotated for landscape reading, per
    the maintainer's own description of what "working" looks like.
    """
    send_image = await _send_path_image(hass, device_id, mock_upload_device, 270)
    endpoint_image = await _render_endpoint_image(hass_client, device_id, 270)

    assert send_image.size == (184, 384), send_image.size
    assert endpoint_image.size == (184, 384), endpoint_image.size
    assert list(endpoint_image.getdata()) == list(send_image.getdata())
