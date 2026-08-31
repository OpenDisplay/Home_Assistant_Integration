"""Tests for the designer's synchronous render endpoint.

Maintainer ruling 2026-08-30: designer preview must never touch anything a
real send would change -- no image-entity update, no SIGNAL_IMAGE_UPDATED
dispatch, no delivery. These tests assert exactly that: the endpoint returns
PNG bytes, dither modes produce different bytes, `prepare_image` receives the
same tone/measured-palette values the send path derives for an unset
tone_compression/measured_palette (adversarial-review finding B1: passing
neither kwarg silently falls back to `prepare_image`'s own different
defaults and renders wrong on any measured-palette panel), and the target's
image entity is provably unchanged before/after a render.
"""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

from opendisplay import DitherMode, prepare_image as real_prepare_image
from PIL import Image as PILImage
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opendisplay.designer.render import DESIGNER_RENDER_URL
from custom_components.opendisplay.services import (
    SCHEMA_DRAWCUSTOM,
    tone_and_measured_palettes_from_call_data,
)

from . import DEVICE_CONFIG

# 0x21 (33) + BWR is a real entry in opendisplay.display_palettes'
# DISPLAY_PALETTE_MAP -- i.e. a panel with a *measured* palette. The base
# DEVICE_CONFIG fixture's panel_ic_type (0) is deliberately NOT in that map,
# so `use_measured_palettes` is a no-op either way for it -- a parity bug
# gated on this flag would pass unnoticed against the default fixture. Every
# test in this module runs against a measured-palette panel instead, via the
# `device_config` override below, so the flag actually matters.
_MEASURED_PALETTE_IC = 0x21


@pytest.fixture
def device_config():
    """Return a device with a real measured-palette panel.

    See _MEASURED_PALETTE_IC.
    """
    measured_display = replace(
        DEVICE_CONFIG.displays[0], panel_ic_type=_MEASURED_PALETTE_IC
    )
    return replace(DEVICE_CONFIG, displays=[measured_display])


@pytest.fixture(autouse=True)
async def setup_entry(hass, mock_config_entry: MockConfigEntry) -> None:
    """Set up the config entry for render-endpoint tests."""
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


def _gradient(width: int, height: int) -> PILImage.Image:
    """Return a non-flat test image.

    A flat color dithers identically regardless of mode, which would make a
    dither-parity assertion pass for the wrong reason.
    """
    img = PILImage.new("RGB", (width, height), (255, 255, 255))
    px = img.load()
    for x in range(width):
        for y in range(height):
            v = int(255 * (x / width))
            px[x, y] = (v, v, v)
    return img


@pytest.fixture
def mock_render():
    """Render a real gradient without invoking the drawing engine."""
    with patch(
        "custom_components.opendisplay.designer.render.generate_image",
        AsyncMock(return_value=_gradient(296, 128)),
    ) as render:
        yield render


async def _post_render(hass_client, **overrides):
    client = await hass_client()
    body = {
        "device_id": overrides.pop("device_id", None),
        "payload": [{"type": "text", "value": "hi", "x": 10, "y": 10}],
        "background": "white",
        "dither": "burkes",
        **overrides,
    }
    return await client.post(DESIGNER_RENDER_URL, json=body)


def _send_paths_tone_and_measured_palettes() -> tuple[float | str, bool]:
    """Return the send path's tone/measured-palette derivation.

    Validates a call carrying neither tone_compression nor measured_palette
    against the real SCHEMA_DRAWCUSTOM and reuses
    tone_and_measured_palettes_from_call_data -- the SAME function
    _drawcustom_for_device itself calls (services.py) -- rather than
    reimplementing the formula here. A send-path change to either the
    schema's defaults or that function moves this test's expectation with
    it, instead of a duplicated formula silently drifting from what the
    send path actually does.
    """
    return tone_and_measured_palettes_from_call_data(SCHEMA_DRAWCUSTOM({"payload": []}))


async def test_render_requires_auth(hass, hass_client_no_auth, device_id, mock_render):
    """The endpoint is authenticated -- no auth, no render."""
    client = await hass_client_no_auth()
    resp = await client.post(
        DESIGNER_RENDER_URL,
        json={"device_id": device_id, "payload": [], "background": "white"},
    )
    assert resp.status == 401


async def test_render_wrong_device_returns_404(hass, hass_client, mock_render):
    """An unknown device_id is a 404, not a 500 or a validation error."""
    resp = await _post_render(hass_client, device_id="not-a-real-device")
    assert resp.status == 404


async def test_render_malformed_payload_returns_400(
    hass, hass_client, device_id, mock_render
):
    """A payload that isn't a list is a 400, not a 500."""
    resp = await _post_render(
        hass_client, device_id=device_id, payload={"not": "a list"}
    )
    assert resp.status == 400


async def test_render_oversized_payload_returns_400(hass, hass_client, device_id):
    """A payload past the element cap is rejected before it reaches generate_image.

    generate_image runs on the event loop (like the send path's own call
    does), so an unbounded payload is an unbounded loop-blocking risk on an
    endpoint that gets called on every debounced edit, not just once per
    deliberate send.
    """
    huge_payload = [{"type": "text", "value": "x", "x": 0, "y": 0}] * 1001
    resp = await _post_render(hass_client, device_id=device_id, payload=huge_payload)
    assert resp.status == 400


async def test_render_missing_device_id_returns_400(hass, hass_client, mock_render):
    """A request with no device_id at all is a 400."""
    client = await hass_client()
    resp = await client.post(DESIGNER_RENDER_URL, json={"payload": []})
    assert resp.status == 400


async def test_render_returns_png_at_the_devices_resolution(
    hass, hass_client, device_id, mock_render
):
    """A successful render responds with real PNG bytes at the panel's size."""
    resp = await _post_render(hass_client, device_id=device_id)
    assert resp.status == 200
    assert resp.content_type == "image/png"
    body = await resp.read()
    assert body.startswith(b"\x89PNG\r\n\x1a\n")

    import io

    decoded = PILImage.open(io.BytesIO(body))
    assert decoded.size == (296, 128)


async def test_render_dither_none_and_ordered_produce_different_bytes(
    hass, hass_client, device_id, mock_render
):
    """Assert parity with the send path.

    The dither mode must actually change the rendered output, the same way
    it changes what a real send would ship.
    """
    none_resp = await _post_render(hass_client, device_id=device_id, dither="none")
    none_body = await none_resp.read()

    ordered_resp = await _post_render(
        hass_client, device_id=device_id, dither="ordered"
    )
    ordered_body = await ordered_resp.read()

    assert none_resp.status == 200
    assert ordered_resp.status == 200
    assert none_body != ordered_body, (
        "dither mode must affect the rendered PNG bytes -- if this fails, "
        "the render endpoint is ignoring the dither field"
    )


async def test_render_does_not_touch_the_image_entity(
    hass, hass_client, device_id, mock_config_entry: MockConfigEntry, mock_render
):
    """Assert the core isolation guarantee.

    A preview render must leave the target's image entity completely
    untouched -- no state change, no picture change, no dispatched signal.
    This is what makes designer play safe next to a live display's real
    dashboard.
    """
    from homeassistant.helpers import entity_registry as er

    entity_registry = er.async_get(hass)
    image_entities = [
        entry
        for entry in entity_registry.entities.values()
        if entry.config_entry_id == mock_config_entry.entry_id
        and entry.domain == "image"
    ]
    assert image_entities
    entity_id = image_entities[0].entity_id

    before = hass.states.get(entity_id)
    assert before is not None

    resp = await _post_render(hass_client, device_id=device_id)
    assert resp.status == 200

    after = hass.states.get(entity_id)
    assert after is not None
    assert after.last_updated == before.last_updated
    assert after.state == before.state
    assert after.attributes.get("entity_picture") == before.attributes.get(
        "entity_picture"
    )


async def test_render_prepare_image_kwargs_match_send_path_derivation(
    hass, hass_client, device_id, mock_render
):
    """Assert B1 (adversarial review).

    prepare_image must get the SEND PATH's tone/measured-palette values for
    an unset tone_compression/measured_palette, not prepare_image's own
    different defaults (tone=0.0, use_measured_palettes=True) -- passing
    neither kwarg silently picks up the latter and renders differently on
    any measured-palette panel.
    """
    with patch(
        "custom_components.opendisplay.designer.render.prepare_image",
        wraps=real_prepare_image,
    ) as prepare_image_spy:
        resp = await _post_render(hass_client, device_id=device_id)
    assert resp.status == 200

    expected_tone, expected_measured = _send_paths_tone_and_measured_palettes()
    kwargs = prepare_image_spy.call_args.kwargs
    assert kwargs["tone"] == expected_tone
    assert kwargs["use_measured_palettes"] == expected_measured


async def test_render_uses_send_paths_defaults_not_prepare_images_own(
    hass, hass_client, device_id, mock_config_entry: MockConfigEntry, mock_render
):
    """Make B1 bite.

    On a REAL measured-palette panel (IC 0x21 + BWR), the endpoint's actual
    output must match what prepare_image(tone="auto",
    use_measured_palettes=False) produces, and must DIFFER from what
    prepare_image's own bare defaults (tone=0.0, use_measured_palettes=True)
    would have produced -- proving the parity gap is pixel-observable, not
    just a kwarg-inspection nicety.
    """
    resp = await _post_render(hass_client, device_id=device_id, dither="ordered")
    assert resp.status == 200
    actual_bytes = await resp.read()

    config = mock_config_entry.runtime_data.device_config
    gradient = _gradient(296, 128)

    _, _, correct = real_prepare_image(
        gradient,
        config=config,
        dither_mode=DitherMode.ORDERED,
        compress=False,
        tone="auto",
        use_measured_palettes=False,
    )
    _, _, buggy = real_prepare_image(
        gradient,
        config=config,
        dither_mode=DitherMode.ORDERED,
        compress=False,
        tone=0.0,
        use_measured_palettes=True,
    )

    import io

    correct_bytes = io.BytesIO()
    correct.save(correct_bytes, format="PNG")
    buggy_bytes = io.BytesIO()
    buggy.save(buggy_bytes, format="PNG")

    assert actual_bytes == correct_bytes.getvalue(), (
        "endpoint output must match the send path's own tone/measured-"
        "palette derivation"
    )
    assert actual_bytes != buggy_bytes.getvalue(), (
        "the measured-palette default gap must be pixel-observable on a "
        "real measured-palette IC, or this test isn't exercising the bug "
        "it's named for"
    )


async def test_render_unmocked_smoke(hass, hass_client, device_id):
    """Exercise the REAL generate_image, not the mocked gradient.

    A wrong odl_renderer kwarg name or a changed signature would pass every
    other test here (generate_image is mocked out) and only show up
    against the real library.
    """
    resp = await _post_render(
        hass_client,
        device_id=device_id,
        payload=[{"type": "text", "value": "hi", "x": 5, "y": 5, "size": 12}],
    )
    assert resp.status == 200
    assert resp.content_type == "image/png"
    body = await resp.read()
    assert body.startswith(b"\x89PNG\r\n\x1a\n")


async def test_render_expands_templates_referencing_an_existing_state(
    hass, hass_client, device_id, mock_render
):
    """Tier-1 adversarial review, finding 1 (root cause).

    docs/drawcustom/supported_types.md documents that field values get HA
    templates expanded before odl-renderer sees them; nothing did that --
    reproduced live with the Load Demo payload's templated icon field
    (a literal ``{{ iif(...) }}`` string reached odl-renderer's icon lookup
    unevaluated: ``Icon '{{ iif(...) }}' not found``). Assert
    generate_image's own ``elements`` kwarg carries the RENDERED value.
    """
    hass.states.async_set("sensor.next_event", "on")
    payload = [
        {
            "type": "icon",
            "value": (
                "{{ 'mdi:thermometer' if is_state('sensor.next_event', 'on')"
                " else 'mdi:calendar-alert' }}"
            ),
            "x": 0,
            "y": 0,
        }
    ]

    resp = await _post_render(hass_client, device_id=device_id, payload=payload)

    assert resp.status == 200
    elements = mock_render.call_args.kwargs["elements"]
    assert elements[0]["value"] == "mdi:thermometer"


async def test_render_template_referencing_a_missing_state_degrades_not_raises(
    hass, hass_client, device_id, mock_render
):
    """A merely-missing entity is the common case, not an error.

    HA's own template functions (is_state/is_state_attr) return a sensible
    default for an entity that doesn't exist yet rather than raising -- this
    must render through cleanly, not be treated as an invalid template.
    """
    payload = [
        {
            "type": "icon",
            "value": (
                "{{ 'mdi:thermometer' if is_state('sensor.does_not_exist', 'on')"
                " else 'mdi:calendar-alert' }}"
            ),
            "x": 0,
            "y": 0,
        }
    ]

    resp = await _post_render(hass_client, device_id=device_id, payload=payload)

    assert resp.status == 200
    elements = mock_render.call_args.kwargs["elements"]
    assert elements[0]["value"] == "mdi:calendar-alert"


async def test_render_broken_template_returns_400_naming_the_element(
    hass, hass_client, device_id, mock_render
):
    """A template that actually raises degrades explicitly, naming the element.

    Never a literal '{{' reaching odl-renderer's icon/text lookup silently,
    and never an opaque traceback from inside odl-renderer either.
    """
    payload = [
        {"type": "text", "value": "hi", "x": 0, "y": 0},
        {
            "type": "icon",
            "value": "{{ this_is_not_a_real_function() }}",
            "x": 0,
            "y": 0,
        },
    ]

    resp = await _post_render(hass_client, device_id=device_id, payload=payload)

    assert resp.status == 400
    body = await resp.json()
    assert "element 1" in body["message"]
    assert "icon" in body["message"]


# --- Virtual-display preview (tier-1 round 2, finding 2) --------------------
#
# The designer's "Virtual display" pick has no HA device behind it at all --
# context.targetId is null, so the panel's renderPreview has no device_id to
# send. These exercise the endpoint's other path: an explicit `display`
# spec (width/height, optionally color_scheme) instead of device_id.


async def _post_render_body(hass_client, body):
    """POST an arbitrary body with no injected defaults (unlike _post_render)."""
    client = await hass_client()
    return await client.post(DESIGNER_RENDER_URL, json=body)


async def test_render_spec_mode_returns_png_at_the_requested_resolution(
    hass, hass_client, mock_render
):
    """A display spec with no device_id renders at exactly the given size."""
    resp = await _post_render_body(
        hass_client,
        {
            "display": {"width": 384, "height": 184},
            "payload": [{"type": "text", "value": "hi", "x": 10, "y": 10}],
        },
    )

    assert resp.status == 200
    assert resp.content_type == "image/png"
    # width/height reach generate_image unchanged (0 rotate -- no transpose).
    kwargs = mock_render.call_args.kwargs
    assert kwargs["width"] == 384
    assert kwargs["height"] == 184


async def test_render_spec_mode_unmocked_smoke(hass, hass_client):
    """Exercise the REAL generate_image/prepare_image for spec mode.

    mock_render fakes generate_image's own output size (296x128, its
    fixture default) -- this proves the endpoint's synthetic GlobalConfig is
    actually accepted by prepare_image for real, not just syntactically
    constructed. The response PNG's own size reflects prepare_image's
    dithered output at the display spec's resolution, not the input image.
    """
    resp = await _post_render_body(
        hass_client,
        {
            "display": {"width": 200, "height": 100},
            "payload": [{"type": "text", "value": "hi", "x": 5, "y": 5, "size": 12}],
        },
    )

    assert resp.status == 200
    assert resp.content_type == "image/png"
    body = await resp.read()
    assert body.startswith(b"\x89PNG\r\n\x1a\n")

    import io

    decoded = PILImage.open(io.BytesIO(body))
    assert decoded.size == (200, 100)


async def test_render_missing_device_id_and_display_returns_400(hass, hass_client):
    """Neither device_id nor display -- a clear 400, not a 500 or a 404."""
    resp = await _post_render_body(
        hass_client,
        {"payload": [{"type": "text", "value": "hi", "x": 0, "y": 0}]},
    )

    assert resp.status == 400
    body = await resp.json()
    assert "device_id" in body["message"]
    assert "display" in body["message"]


async def test_render_device_mode_still_works_unchanged(
    hass, hass_client, device_id, mock_render
):
    """Adding spec mode must not disturb the existing device_id path."""
    resp = await _post_render(hass_client, device_id=device_id)

    assert resp.status == 200
    assert resp.content_type == "image/png"


async def test_render_serves_an_authenticated_non_admin_user(
    hass, hass_client, hass_read_only_access_token, device_id, mock_render
):
    """Any authenticated user can render -- deliberately, not by oversight.

    Auth and display are kept consistent (@schlomo's ruling): the sidebar
    panel is offered to every authenticated user, so the endpoints behind it
    answer every authenticated user too. The endpoint grants no capability a
    non-admin does not already have -- `opendisplay.drawcustom` renders the
    same templates through the same shared helper, and Home Assistant
    templates are read-only. Restricting the designer is a Home Assistant
    user-level decision, not one this integration invents a permission model
    for.

    `test_render_requires_auth` above pins the other half of the contract:
    an unauthenticated request is still rejected.
    """
    client = await hass_client(hass_read_only_access_token)
    resp = await client.post(
        DESIGNER_RENDER_URL,
        json={
            "device_id": device_id,
            "payload": [{"type": "text", "value": "hi", "x": 10, "y": 10}],
            "background": "white",
            "dither": "burkes",
        },
    )

    assert resp.status == 200
    assert resp.content_type == "image/png"
