"""Tests for the designer's static asset view and sidebar panel registration."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opendisplay.const import DOMAIN
from custom_components.opendisplay.designer.panel import (
    DESIGNER_PANEL_PATH,
    DESIGNER_STATIC_URL,
)


@pytest.fixture(autouse=True)
async def setup_entry(hass, mock_config_entry: MockConfigEntry) -> None:
    """Set up the config entry so async_setup_designer has run."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()


async def test_panel_js_is_served(hass, hass_client_no_auth):
    """The panel JS is reachable, unauthenticated, with a JS content type."""
    client = await hass_client_no_auth()
    resp = await client.get(
        f"{DESIGNER_STATIC_URL}/panel/opendisplay-designer-panel.js"
    )
    assert resp.status == 200
    assert "javascript" in resp.content_type
    body = await resp.text()
    assert "opendisplay-designer-panel" in body


async def test_vendor_library_is_served(hass, hass_client_no_auth):
    """The vendored designer library itself is reachable through the same view."""
    client = await hass_client_no_auth()
    resp = await client.get(f"{DESIGNER_STATIC_URL}/vendor/odl-drawcustom-designer.js")
    assert resp.status == 200


async def test_panel_js_vendor_imports_carry_a_cache_busting_token(
    hass, hass_client_no_auth
):
    """Assert panel.js's vendor imports get a cache-busting token too.

    A bare ES-module import specifier doesn't inherit its importer's own
    `?v=` query (the browser resolves it fresh, with no query at all), so
    panel.js's vendor imports get rewritten at serve time to carry the same
    cache-busting token panel.js's own URL does -- otherwise the ~5.6MB
    designer bundle they pull in never benefits from the long-cache headers
    a `?v=`-tokened URL gets (see the view's own Cache-Control logic).
    """
    client = await hass_client_no_auth()
    resp = await client.get(
        f"{DESIGNER_STATIC_URL}/panel/opendisplay-designer-panel.js"
    )
    body = await resp.text()
    assert "from '../vendor/odl-drawcustom-designer.js?v=" in body
    assert "from '../vendor/js-yaml.mjs?v=" in body


async def test_unknown_static_file_is_404(hass, hass_client_no_auth):
    """A path with no matching file is a 404, not a 500."""
    client = await hass_client_no_auth()
    resp = await client.get(f"{DESIGNER_STATIC_URL}/panel/does-not-exist.js")
    assert resp.status == 404


async def test_path_traversal_is_forbidden(hass, hass_client_no_auth):
    """A `..`-carrying path is rejected before any file access is attempted."""
    client = await hass_client_no_auth()
    resp = await client.get(f"{DESIGNER_STATIC_URL}/../../../etc/passwd")
    # aiohttp normalizes the URL path before routing reaches our view for a
    # literal "..", so this either 404s (no matching route/file) or comes
    # back 403 from the view's own guard -- either way, never a 200 serving
    # a file outside designer/frontend/.
    assert resp.status in (403, 404)


async def test_panel_registered_in_frontend(hass) -> None:
    """async_setup_designer's panel_custom registration actually landed."""
    assert DOMAIN in hass.data
    designer_data = hass.data[DOMAIN].get("designer", {})
    assert designer_data.get("panel_registered") is True


async def test_panel_is_offered_to_every_authenticated_user(hass) -> None:
    """The panel is not admin-only -- deliberately.

    Auth and display are kept consistent (@schlomo's ruling): the panel's
    visibility matches the authorization of the endpoints behind it, which
    in turn matches the exposure of the `opendisplay.drawcustom` service the
    designer fronts. A deployment that wants the designer restricted
    restricts it at the Home Assistant user level; the integration does not
    invent its own permission model. See docs/designer.md's "Access and
    exposure".
    """
    panel = hass.data["frontend_panels"][DESIGNER_PANEL_PATH]
    assert panel.require_admin is False


async def test_static_view_stays_unauthenticated(hass) -> None:
    """The static view cannot be gated -- a module import sends no auth header.

    `<script type="module">`/`import` requests carry no Authorization header
    and no HA auth cookie, so requiring auth here would make the panel
    unloadable for everyone. It serves only this integration's own bundled
    frontend files -- the panel's own JS bundle -- and exposes no Home
    Assistant data, no configuration and no entity state. The endpoints that
    do (`render`, `asset`) require auth.
    """
    from custom_components.opendisplay.designer.panel import (
        OpenDisplayDesignerStaticView,
    )

    assert OpenDisplayDesignerStaticView.requires_auth is False
