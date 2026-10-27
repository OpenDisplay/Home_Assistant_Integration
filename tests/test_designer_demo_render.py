"""Render the designer's Load Demo payload through a real Home Assistant.

The designer previews templates with nunjucks (JavaScript); Home Assistant
renders them with Python Jinja. The two disagree on more than syntax: a
real send of the Load Demo payload failed on a live Home Assistant because
its ``progress_bar`` divided ``now().strftime('%S')`` -- a string -- by 60.
nunjucks coerced silently; HA raised ``TypeError: unsupported operand
type(s) for /: 'str' and 'int'``. Nothing in either repository rendered the
demo through Python, so the bug reached a display before anyone saw it
(fixed designer-side in odl-drawcustom-designer#196).

The vendored designer package ships its demo payload (``showcase.yml``) and
State Simulator seed (``showcase.json``) since designer 3.5.0
(``scripts/update-designer-vendor.py`` copies both into ``vendor/``). This
module runs that payload through the panel's shipped send procedure
(maintainer decision 2026-10-27): the designer's State Simulator variables
are the analog of an automation's script-level ``variables:``, so the panel
pre-renders every templated field with those variables as Jinja context --
what HA's script engine does with ``template.render_complex(data,
variables)`` before a service handler runs -- and then calls the untouched
``opendisplay.drawcustom`` service with literal YAML. ``_prerender`` below
is that first step, using HA's own ``render_complex``; the render endpoint
and a ``dry-run`` service call are the second, at the three fabricated
display geometries ``dev/inject-displays.py`` seeds. That is a real send
minus BLE delivery; what a physical panel's dither/palette makes of the PNG
is outside its reach.

The demo's ``dlimg`` once referenced ``/local/showcase.png``, which the
designer served from its own bundle but odl-renderer opened as a filesystem
path that exists on no Home Assistant install (``Image file not found:
/local/showcase.png``). Since designer 3.6.0 the image travels inside the
payload as a ``data:`` URI (odl-drawcustom-designer#198), so the whole demo
-- image included -- renders here; ``test_demo_bundled_image_is_self_contained``
keeps it that way.
"""

from dataclasses import replace
import io
import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import (
    Template,
    TemplateError,
    is_template_string,
    render_complex,
)
from odl_renderer.colors import WHITE, ColorResolver
from opendisplay import ColorScheme
from PIL import Image as PILImage
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import yaml

from custom_components.opendisplay.const import DOMAIN
from custom_components.opendisplay.designer.render import DESIGNER_RENDER_URL
from custom_components.opendisplay.services import render_payload_templates

from . import DEVICE_CONFIG

VENDOR_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "opendisplay"
    / "designer"
    / "frontend"
    / "vendor"
)

# The geometries dev/inject-displays.py seeds into a disposable HA, so the
# demo renders at every size and colour scheme a developer can see live.
# The first is also the conftest default (tests/__init__.py DEVICE_CONFIG).
_GEOMETRIES = {
    "296x128-BWR": (296, 128, ColorScheme.BWR),
    "200x200-MONO": (200, 200, ColorScheme.MONO),
    "800x480-BWRY": (800, 480, ColorScheme.BWRY),
}
_DEFAULT_GEOMETRY = "296x128-BWR"
_ALL_GEOMETRIES = pytest.mark.parametrize(
    "device_config", sorted(_GEOMETRIES), indirect=True, ids=sorted(_GEOMETRIES)
)

_TEMPLATE_LOGGER = "homeassistant.helpers.template"


@pytest.fixture
def device_config(request):
    """Override the conftest device with one fabricated display geometry."""
    width, height, scheme = _GEOMETRIES[getattr(request, "param", _DEFAULT_GEOMETRY)]
    display = replace(
        DEVICE_CONFIG.displays[0],
        pixel_width=width,
        pixel_height=height,
        color_scheme=scheme.value,
    )
    return replace(DEVICE_CONFIG, displays=[display])


def _vendored(name: str) -> str:
    path = VENDOR_DIR / name
    assert path.is_file(), (
        f"{path} is missing -- the vendored designer predates the demo bundle; "
        "run scripts/update-designer-vendor.py --pin <version >= 3.5.0>"
    )
    return path.read_text("utf-8")


@pytest.fixture
def demo_payload() -> list[dict[str, Any]]:
    """Return the vendored Load Demo payload, exactly as the designer ships it."""
    payload = yaml.safe_load(_vendored("showcase.yml"))
    assert isinstance(payload, list)
    assert payload
    return payload


@pytest.fixture
def demo_simulator() -> dict[str, Any]:
    """Return the vendored State Simulator seed (states, attributes, variables)."""
    return json.loads(_vendored("showcase.json"))["simulator"]


@pytest.fixture
async def seeded_hass(hass, demo_simulator):
    """Seed hass with the demo's simulator states and attributes.

    This is the state the designer's own preview renders against. The
    seed's ``variables`` are not HA state; ``_prerender`` applies them the
    way the panel does.
    """
    attributes = demo_simulator.get("attributes", {})
    for entity_id, state in demo_simulator["states"].items():
        hass.states.async_set(entity_id, state, attributes.get(entity_id))
    await hass.async_block_till_done()
    return hass


@pytest.fixture(autouse=True)
async def setup_entry(hass, mock_config_entry: MockConfigEntry) -> None:
    """Set up the config entry for the fabricated display."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def device_id(hass, mock_config_entry: MockConfigEntry) -> str:
    """Return the device registry ID for the fabricated display."""
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    assert devices
    return devices[0].id


def _as_templates(hass: HomeAssistant, value: Any) -> Any:
    """Turn every template string in a field value into a ``Template`` leaf.

    ``cv.template`` does this at config load for an automation's service
    data; ``render_complex`` renders only ``Template`` leaves.
    """
    if isinstance(value, str):
        return Template(value, hass) if is_template_string(value) else value
    if isinstance(value, list):
        return [_as_templates(hass, item) for item in value]
    if isinstance(value, dict):
        return {key: _as_templates(hass, item) for key, item in value.items()}
    return value


def _prerender(
    hass: HomeAssistant, payload: list[dict[str, Any]], variables: dict[str, Any]
) -> list[dict[str, Any]]:
    """Render the payload's templates with the designer's variables, field by field.

    The panel's shipped send procedure, step one: HA's own
    ``render_complex`` -- what the script engine runs on an automation's
    service data -- with the State Simulator variables as Jinja context. A
    field whose template raises fails the test naming element index, type
    and field, mirroring the live send's error text.
    """
    rendered = []
    for index, element in enumerate(payload):
        out = {}
        for key, value in element.items():
            try:
                out[key] = render_complex(_as_templates(hass, value), variables)
            except TemplateError as err:
                pytest.fail(
                    f"payload element {index} (type '{element.get('type', '?')}')"
                    f" field '{key}' has an invalid template: {err}"
                )
        rendered.append(out)
    return rendered


@pytest.fixture
def prerendered_demo(seeded_hass, demo_payload, demo_simulator):
    """Return the demo as the panel hands it to the service: templates already literal."""
    return _prerender(seeded_hass, demo_payload, demo_simulator["variables"])


async def _render(hass_client, device_id: str, payload: list[dict[str, Any]]):
    client = await hass_client()
    return await client.post(
        DESIGNER_RENDER_URL,
        json={"device_id": device_id, "payload": payload, "background": "white"},
    )


async def test_demo_prerenders_through_ha_jinja_with_its_variables(
    seeded_hass, demo_payload, demo_simulator, caplog
):
    """Every demo template evaluates in HA's Python Jinja with the demo's variables.

    A failure names the element index, type, field and Jinja's own reason,
    the way the live send did: ``payload element 19 (type 'progress_bar')
    field 'progress' has an invalid template: TypeError: unsupported
    operand type(s) for /: 'str' and 'int'`` (designer 3.4.9's showcase).

    The panel renders strict, so a variable the demo references but does
    not define is a failure here too: HA's non-strict ``render_complex``
    degrades it to '' with a template warning, which this test forbids.
    """
    caplog.set_level(logging.WARNING, logger=_TEMPLATE_LOGGER)
    variables = demo_simulator["variables"]
    assert variables == {"accent_color": "red", "alert": "true"}, (
        "the demo's variables changed -- re-derive the expectations below"
    )

    rendered = _prerender(seeded_hass, demo_payload, variables)

    assert len(rendered) == len(demo_payload)
    undefined = [
        r.getMessage()
        for r in caplog.records
        if r.name == _TEMPLATE_LOGGER and "is undefined" in r.getMessage()
    ]
    assert undefined == []
    # The variables actually took effect: the accent icon fill and the
    # title colour are the designer's red, and the alert-gated circle fill
    # took its truthy branch.
    assert rendered[15]["type"] == "icon"
    assert rendered[15]["fill"] == "red"
    assert rendered[20]["type"] == "text"
    assert rendered[20]["color"] == "red"
    assert rendered[6]["type"] == "circle"
    assert rendered[6]["fill"] == "red"
    # No template syntax survives into what the service will receive.
    for element in rendered:
        for value in element.values():
            assert not (isinstance(value, str) and is_template_string(value))


@_ALL_GEOMETRIES
async def test_prerendered_demo_renders_to_png_at_every_fabricated_geometry(
    hass_client, device_id, prerendered_demo, device_config
):
    """The pre-rendered demo renders through odl-renderer to a PNG of the display's size.

    The render endpoint shares the send path's template expansion, local
    image preload and ``generate_image`` call, so this is a real send minus
    BLE delivery.
    """
    resp = await _render(hass_client, device_id, prerendered_demo)

    if resp.status != 200:
        pytest.fail(f"render returned {resp.status}: {await resp.text()}")
    assert resp.content_type == "image/png"
    decoded = PILImage.open(io.BytesIO(await resp.read()))
    display = device_config.displays[0]
    assert decoded.size == (display.pixel_width, display.pixel_height)


@_ALL_GEOMETRIES
async def test_prerendered_demo_dry_run_sends_at_every_fabricated_geometry(
    seeded_hass, device_id, prerendered_demo, device_config
):
    """The untouched drawcustom service accepts the pre-rendered demo (dry run).

    Step two of the panel's send procedure, through the real service
    handler: schema validation, template expansion (a no-op on literal
    YAML), render and prepare for the device -- everything but the upload.
    """
    response = await seeded_hass.services.async_call(
        DOMAIN,
        "drawcustom",
        {
            "device_id": [device_id],
            "payload": prerendered_demo,
            "dry-run": True,
        },
        blocking=True,
        return_response=True,
    )

    assert response["status"] == "dry_run"


def test_demo_bundled_image_is_self_contained(demo_payload):
    """The demo's image travels inside the payload, not as a path on the HA host.

    odl-renderer resolves a ``dlimg`` url with a leading ``/`` on the Home
    Assistant host's filesystem, and the designer's bundle lives nowhere
    there: designer 3.4.9 shipped ``/local/showcase.png`` and the demo
    failed with ``Image file not found``. A ``data:`` URI needs no file.
    The geometry tests above prove it decodes; this guards the shape so a
    future demo edit cannot reintroduce a host path unnoticed.
    """
    images = [element for element in demo_payload if element["type"] == "dlimg"]

    assert images, "the demo stopped shipping an image; drop this guard knowingly"
    for element in images:
        assert element["url"].startswith("data:image/"), element["url"]


async def test_raw_send_renders_designer_variables_empty(
    seeded_hass, demo_payload, caplog
):
    """Why the panel pre-renders: a raw send knows nothing of designer variables.

    The service's own template expansion runs with no variables, so HA
    renders ``{{ accent_color }}`` as undefined -> '' and the alert-gated
    fill takes its else branch; odl-renderer then resolves '' as an unknown
    colour name and paints it white -- the accent icon and the title vanish
    on a white background while the preview shows them red. The panel's
    pre-render (``_prerender``) is what keeps that from reaching a device.
    """
    caplog.set_level(logging.WARNING, logger=_TEMPLATE_LOGGER)

    rendered = render_payload_templates(seeded_hass, demo_payload)

    assert rendered[15]["fill"] == ""
    assert rendered[20]["color"] == ""
    assert rendered[6]["fill"] == "white"
    warnings = [r.getMessage() for r in caplog.records if r.name == _TEMPLATE_LOGGER]
    assert any("'accent_color' is undefined" in w for w in warnings)
    assert any("'alert' is undefined" in w for w in warnings)
    assert ColorResolver().resolve("") == WHITE
