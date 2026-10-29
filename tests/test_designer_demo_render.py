"""Render this integration's Load Demo payload through a real Home Assistant.

The designer previews templates with nunjucks (JavaScript); Home Assistant
renders them with Python Jinja. The two disagree on more than syntax: a
real send of the designer's bundled Load Demo failed on a live Home
Assistant because its ``progress_bar`` divided ``now().strftime('%S')`` --
a string -- by 60. nunjucks coerced silently; HA raised ``TypeError:
unsupported operand type(s) for /: 'str' and 'int'``. Nothing in either
repository rendered the demo through Python, so the bug reached a display
before anyone saw it (fixed designer-side in odl-drawcustom-designer#196).

**This integration ships its own Load Demo** (maintainer decision
2026-10-28): ``designer/frontend/demo/demo.yml`` (the drawcustom payload)
and ``demo.json`` (its variables), which the panel hands the designer as the
``demo`` mount option so Load Demo in Home Assistant loads *this* payload,
not the designer's bundled showcase. Two things about the bundled showcase
made it the wrong demo for Home Assistant:

- It references fabricated entities (``sensor.temperature``,
  ``weather.forecast_home``, ``binary_sensor.door``, ``sensor.next_event``)
  that the designer's State Simulator seeds and a real Home Assistant does
  not have, so fields rendered "unknown" on a real install.
- Its ``plot`` element needs recorder history for a numeric sensor, which
  no entity on every install has -- the live send failed with ``Element 19:
  no data returned for entity: sensor.temperature``. The plot is dropped
  from the demo (the element stays available from the editor's insert
  menu).

The HA demo references only entities every Home Assistant install has
(``sun.sun``, ``zone.home``, the ``person`` domain) plus template globals
such as ``now()``. This module renders it through the panel's shipped send
procedure (maintainer decision 2026-10-27; ``template-prerender.js``,
modelled here with HA's own template engine, not imported from the panel
JS): the designer's variables are the analog of an automation's script-level
``variables:``,
so the panel pre-renders every templated field with those variables as
Jinja context -- what HA's script engine does with
``template.render_complex(data, variables)`` before a service handler runs
-- and then calls the untouched ``opendisplay.drawcustom`` service with
literal YAML. ``_prerender`` below is that first step, strict (an undefined
variable fails instead of degrading to ''); the render endpoint and a
``dry-run`` service call are the second, at the three fabricated display
geometries ``dev/inject-displays.py`` seeds. That is a real send minus BLE
delivery; what a physical panel's dither/palette makes of the PNG is
outside its reach.

The harness Home Assistant seeds **no states**: it sets up the ``sun``,
``zone`` and ``person`` integrations exactly as every install does and
nothing else, so ``test_demo_references_only_entities_every_install_has``
fails the moment a demo edit reintroduces a fabricated sensor. The demo's
``dlimg`` travels inside the payload as a ``data:`` URI (the designer's
showcase once referenced ``/local/showcase.png``, which exists on no Home
Assistant host); ``test_demo_bundled_image_is_self_contained`` keeps it
that way.
"""

from dataclasses import replace
import io
import json
import logging
from pathlib import Path
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template, TemplateError, is_template_string
from homeassistant.setup import async_setup_component
from odl_renderer.colors import WHITE, ColorResolver
from odl_renderer.types import ElementType
from opendisplay import ColorScheme
from PIL import Image as PILImage
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import yaml

from custom_components.opendisplay.const import DOMAIN
from custom_components.opendisplay.designer.render import DESIGNER_RENDER_URL
from custom_components.opendisplay.services import render_payload_templates

from . import DEVICE_CONFIG

# The one copy of the demo: the panel fetches these two files from the
# designer static view (frontend/panel/host-demo.js), this module reads
# them from disk.
DEMO_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "opendisplay"
    / "designer"
    / "frontend"
    / "demo"
)

# The integrations the demo's entities come from. ``person`` and ``zone``
# are in Home Assistant's core bootstrap (``bootstrap.DEFAULT_INTEGRATIONS``);
# ``sun`` is a ``default_config`` dependency, so every onboarded install has
# it unless its configuration.yaml drops ``default_config:``. Setting them up
# is the whole of the harness's entity population.
_STOCK_INTEGRATIONS = ("sun", "zone", "person")
# What a configuration.yaml without ``default_config:`` still has.
_CORE_BOOTSTRAP_INTEGRATIONS = ("zone", "person")

# Element types odl-renderer knows that the demo deliberately lacks: the
# plot (needs recorder history no stock entity has) and the diagram (the
# designer has no such element -- its bundle does not contain the word).
_ELEMENT_TYPES_NOT_IN_DEMO = {"plot", "diagram"}

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

# A quoted entity id inside a template: 'sun.sun', "zone.home". Font names
# (rbm.ttf) and icons (mdi:home) are never quoted that way in the demo.
_QUOTED_ENTITY_ID = re.compile(r"""['"]([a-z_]+\.[a-z0-9_]+)['"]""")
# An unquoted entity access: states.sun.sun, states.zone.home.state.
_UNQUOTED_ENTITY_ID = re.compile(r"\bstates\.([a-z_]+\.[a-z0-9_]+)\b")
# A domain iteration: states.person, states.sensor (no object id after it).
_DOMAIN_ITERATION = re.compile(r"\bstates\.([a-z_]+)\b(?!\.[a-z0-9_])")


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


def _demo_file(name: str) -> str:
    path = DEMO_DIR / name
    assert path.is_file(), f"{path} is missing -- the integration's Load Demo"
    return path.read_text("utf-8")


@pytest.fixture
def demo_payload() -> list[dict[str, Any]]:
    """Return the Load Demo payload, exactly as the panel hands it to the designer."""
    payload = yaml.safe_load(_demo_file("demo.yml"))
    assert isinstance(payload, list)
    assert payload
    return payload


@pytest.fixture
def demo_variables() -> dict[str, str]:
    """Return the Load Demo's variables (the designer's Variables tab seed)."""
    variables = json.loads(_demo_file("demo.json"))["variables"]
    assert isinstance(variables, dict)
    assert all(isinstance(value, str) for value in variables.values()), (
        "MountOptions.demo.variables is Record<string, string>"
    )
    return variables


@pytest.fixture
async def stock_hass(hass):
    """Return hass with only the integrations every install has, and no seeded state.

    ``sun``, ``zone`` and ``person`` create ``sun.sun`` and ``zone.home``
    from the harness's own config (latitude/longitude, home name) the way a
    fresh install does. Nothing here calls ``hass.states.async_set``: a
    state the demo needs that a real install would not have is a demo bug,
    and seeding it would hide that.
    """
    for domain in _STOCK_INTEGRATIONS:
        assert await async_setup_component(hass, domain, {domain: {}})
    await hass.async_block_till_done()
    return hass


@pytest.fixture
async def core_only_hass(hass):
    """Return hass as a configuration.yaml without ``default_config:`` leaves it: no ``sun``."""
    for domain in _CORE_BOOTSTRAP_INTEGRATIONS:
        assert await async_setup_component(hass, domain, {domain: {}})
    await hass.async_block_till_done()
    assert hass.states.get("sun.sun") is None
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


def _render_strict(hass: HomeAssistant, value: Any, variables: dict[str, Any]) -> Any:
    """Render every template string in a field value, strictly.

    The structural walk is ``render_complex``'s (a ``Template`` leaf per
    template string, lists and dicts recursed); the leaf render is
    ``strict=True``, which the panel's pre-render is too: an undefined
    variable raises instead of degrading to '' with a warning.
    """
    if isinstance(value, str):
        if not is_template_string(value):
            return value
        return Template(value, hass).async_render(variables, strict=True)
    if isinstance(value, list):
        return [_render_strict(hass, item, variables) for item in value]
    if isinstance(value, dict):
        return {
            key: _render_strict(hass, item, variables) for key, item in value.items()
        }
    return value


def _prerender(
    hass: HomeAssistant, payload: list[dict[str, Any]], variables: dict[str, Any]
) -> list[dict[str, Any]]:
    """Render the payload's templates with the demo's variables, field by field.

    Step one of the panel's shipped send procedure, modelled with HA's own
    template engine: the demo's variables as Jinja context, strict like the
    panel (``render_template`` with ``strict: true``).
    A field whose template raises fails the test naming element index, type
    and field, mirroring the live send's error text.
    """
    rendered = []
    for index, element in enumerate(payload):
        out = {}
        for key, value in element.items():
            try:
                out[key] = _render_strict(hass, value, variables)
            except TemplateError as err:
                pytest.fail(
                    f"payload element {index} (type '{element.get('type', '?')}')"
                    f" field '{key}' has an invalid template: {err}"
                )
        rendered.append(out)
    return rendered


@pytest.fixture
def prerendered_demo(stock_hass, demo_payload, demo_variables):
    """Return the demo as the panel hands it to the service: templates already literal."""
    return _prerender(stock_hass, demo_payload, demo_variables)


def _only(
    payload: list[dict[str, Any]], element_type: str, field: str, needle: str
) -> int:
    """Return the index of the one element of a type whose field mentions a needle."""
    hits = [
        index
        for index, element in enumerate(payload)
        if element["type"] == element_type and needle in str(element.get(field, ""))
    ]
    assert len(hits) == 1, (element_type, field, needle, hits)
    return hits[0]


def _variable_driven_fields(payload: list[dict[str, Any]]) -> dict[str, int]:
    """Return the indices of the demo elements whose colour the variables drive.

    The accent icon's fill and the title's colour are ``{{ accent_color }}``;
    the circle's fill is gated on ``alert``. Found by template text rather
    than by position so a demo edit that moves elements does not silently
    retarget these assertions.
    """
    return {
        "icon": _only(payload, "icon", "fill", "accent_color"),
        "title": _only(payload, "text", "color", "accent_color"),
        "circle": _only(payload, "circle", "fill", "alert"),
    }


async def _render(hass_client, device_id: str, payload: list[dict[str, Any]]):
    client = await hass_client()
    return await client.post(
        DESIGNER_RENDER_URL,
        json={"device_id": device_id, "payload": payload, "background": "white"},
    )


def _template_strings(value: Any):
    """Yield every template string in a field value, nested lists and dicts included.

    The same walk ``_render_strict`` (and HA's ``render_complex``) does, so
    a template hidden in a list or a nested mapping is scanned too.
    """
    if isinstance(value, str):
        if is_template_string(value):
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from _template_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _template_strings(item)


def _referenced_entities(payload: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Return every (entity id, iterated domain) the demo's templates name.

    Entity ids are matched both quoted (``states('sun.sun')``,
    ``is_state("zone.home", ...)``) and unquoted (``states.sun.sun``); a
    bare ``states.person`` is a domain iteration.
    """
    entity_ids: set[str] = set()
    domains: set[str] = set()
    for template in _template_strings(payload):
        entity_ids.update(_QUOTED_ENTITY_ID.findall(template))
        entity_ids.update(_UNQUOTED_ENTITY_ID.findall(template))
        domains.update(_DOMAIN_ITERATION.findall(template))
    return entity_ids, domains


def test_referenced_entities_scans_nested_fields_and_unquoted_access():
    """The scanner sees what the renderer renders: nested fields, both reference styles."""
    payload = [
        {
            "type": "text",
            "value": "{{ states('sun.sun') }}",
            "nested": {"deep": ["{{ states.zone.home.state }}", "literal"]},
            "points": [[1, 2], [3, 4]],
        },
        {
            "type": "icon",
            "value": "{{ states.person | list | count }} {{ states.sensor }}",
        },
        {
            "type": "text",
            "value": "{{ is_state_attr(\"binary_sensor.door\", 'on', true) }}",
        },
    ]

    entity_ids, domains = _referenced_entities(payload)

    assert entity_ids == {"sun.sun", "zone.home", "binary_sensor.door"}
    assert domains == {"person", "sensor"}


async def test_demo_references_only_entities_every_install_has(
    stock_hass, demo_payload
):
    """Every entity the demo names exists in a Home Assistant with no seeded state.

    The harness sets up ``sun``, ``zone`` and ``person`` and seeds nothing
    else (``stock_hass``), so a fabricated sensor cannot creep back into
    the demo: the designer's bundled showcase referenced four of them and
    every one rendered "unknown" on a real install.
    """
    entity_ids, domains = _referenced_entities(demo_payload)

    assert entity_ids, (
        "the demo stopped referencing any entity; drop this guard knowingly"
    )
    missing = sorted(eid for eid in entity_ids if stock_hass.states.get(eid) is None)
    assert missing == [], f"demo references entities a stock install lacks: {missing}"
    assert domains <= set(_STOCK_INTEGRATIONS), (
        f"demo iterates a domain not every install has: {sorted(domains)}"
    )


async def test_demo_prerenders_through_ha_jinja_with_its_variables(
    stock_hass, demo_payload, demo_variables, caplog
):
    """Every demo template evaluates in HA's Python Jinja with the demo's variables.

    A failure names the element index, type, field and Jinja's own reason,
    the way the live send did: ``payload element 19 (type 'progress_bar')
    field 'progress' has an invalid template: TypeError: unsupported
    operand type(s) for /: 'str' and 'int'`` (designer 3.4.9's showcase).

    The render is strict, as the panel's pre-render is, so
    a variable the demo references but does not define raises here too; a
    template that merely warns (a missing attribute, an unknown filter
    argument) fails as well, because a demo that logs warnings on every
    Load Demo is not a clean demo.
    """
    caplog.set_level(logging.WARNING, logger=_TEMPLATE_LOGGER)
    assert demo_variables == {"accent_color": "red", "alert": "true"}, (
        "the demo's variables changed -- re-derive the expectations below"
    )

    rendered = _prerender(stock_hass, demo_payload, demo_variables)

    assert len(rendered) == len(demo_payload)
    warnings = [r.getMessage() for r in caplog.records if r.name == _TEMPLATE_LOGGER]
    assert warnings == []
    # The variables actually took effect: the accent icon fill and the
    # title colour are the demo's red, and the alert-gated circle fill took
    # its truthy branch.
    at = _variable_driven_fields(demo_payload)
    assert rendered[at["icon"]]["fill"] == "red"
    assert rendered[at["title"]]["color"] == "red"
    assert rendered[at["circle"]]["fill"] == "red"
    # No template syntax survives into what the service will receive, and
    # no state-backed field degraded to HA's "unknown"/"unavailable".
    for element in rendered:
        for value in element.values():
            assert not (isinstance(value, str) and is_template_string(value))
            if isinstance(value, str):
                for missing in ("unknown", "unavailable", "None"):
                    assert missing not in value, element


async def test_demo_degrades_without_default_config_instead_of_failing(
    core_only_hass, demo_payload, demo_variables
):
    """On an install without ``default_config:`` (no ``sun``) the demo still sends.

    ``state_attr('sun.sun', ...)`` is ``None`` there, and ``round``/
    ``as_timestamp`` raise on ``None`` -- a first cut of this demo failed
    such a send with ``round got invalid input 'None'``. The demo passes
    each of them a default, so the sun fields read "?"/"unknown" and the
    send goes through. Strict, as the real pre-render is.
    """
    rendered = _prerender(core_only_hass, demo_payload, demo_variables)

    sun_fields = [
        element["value"]
        for element in rendered
        if element["type"] == "text" and "Sun" in str(element.get("value", ""))
    ]
    assert sun_fields, "the demo stopped showing the sun; re-derive this test"
    assert any("?" in value or "unknown" in value for value in sun_fields), sun_fields


def test_demo_has_no_plot_element(demo_payload):
    """The demo ships without a plot: no entity every install has carries history.

    A ``plot`` reads recorder history for a numeric sensor; the bundled
    showcase's one failed the live send with ``no data returned for
    entity: sensor.temperature``. The element itself stays available from
    the editor's insert menu -- it is only not in the demo.
    """
    assert [e["type"] for e in demo_payload if e["type"] == "plot"] == []


def test_demo_shows_every_other_element_type(demo_payload):
    """The demo demonstrates every element type odl-renderer knows, bar plot and diagram.

    Derived from the pinned odl-renderer's ``ElementType`` rather than a
    hand-kept list, so a renderer release that adds an element type fails
    this test until the demo shows it (or the exclusion set says why not).
    ``plot`` is excluded by design (no stock entity has recorder history);
    ``diagram`` because the designer has no such element -- verified against
    the vendored bundle, which does not contain the word.
    """
    renderer_types = {member.value for member in ElementType}
    assert renderer_types >= _ELEMENT_TYPES_NOT_IN_DEMO

    assert {
        e["type"] for e in demo_payload
    } == renderer_types - _ELEMENT_TYPES_NOT_IN_DEMO


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
    stock_hass, device_id, prerendered_demo, device_config
):
    """The untouched drawcustom service accepts the pre-rendered demo (dry run).

    Step two of the panel's send procedure, through the real service
    handler: schema validation, template expansion (a no-op on literal
    YAML), render and prepare for the device -- everything but the upload.
    """
    response = await stock_hass.services.async_call(
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
    Assistant host's filesystem, and nothing of the designer's lives there:
    designer 3.4.9's showcase shipped ``/local/showcase.png`` and the demo
    failed with ``Image file not found``. A ``data:`` URI needs no file.
    The geometry tests above prove it decodes; this guards the shape so a
    future demo edit cannot reintroduce a host path unnoticed.
    """
    images = [element for element in demo_payload if element["type"] == "dlimg"]

    assert images, "the demo stopped shipping an image; drop this guard knowingly"
    for element in images:
        assert element["url"].startswith("data:image/"), element["url"]


async def test_raw_send_renders_designer_variables_empty(
    stock_hass, demo_payload, caplog
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

    rendered = render_payload_templates(stock_hass, demo_payload)

    at = _variable_driven_fields(demo_payload)
    assert rendered[at["icon"]]["fill"] == ""
    assert rendered[at["title"]]["color"] == ""
    assert rendered[at["circle"]]["fill"] == "white"
    warnings = [r.getMessage() for r in caplog.records if r.name == _TEMPLATE_LOGGER]
    assert any("'accent_color' is undefined" in w for w in warnings)
    assert any("'alert' is undefined" in w for w in warnings)
    assert ColorResolver().resolve("") == WHITE
