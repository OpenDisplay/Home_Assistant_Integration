"""Tests for the designer's asset-resolution endpoint (issue #138 last tier).

Maintainer ruling (tier-2, real hardware): "if the server renderer can use
it, the client must get it mapped" -- font-only v1 (see
`custom_components/opendisplay/designer/asset.py`'s own module doc for why
images aren't included). Auth, 404, path-traversal and content-type are
exercised directly against the HTTP view; the live-harness check (drop a
real font file into a search dir, confirm the endpoint serves the exact same
bytes `_font_search_dirs`/`FontManager` would load for a canvas render --
the maintainer's Tinos-Bold case) is covered by `test_asset_serves_a_real_font_file_bytes_for_bytes`,
run against a real temp directory rather than mocked I/O.
"""

from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opendisplay.designer.asset import (
    DESIGNER_ASSET_URL,
    _resolve_font_path,
)

# A minimal, syntactically-real TrueType font is not worth fabricating byte
# for byte here -- these tests only need SOME distinguishable bytes under a
# known name, not a font `PIL.ImageFont` could actually shape text with (the
# render endpoint's own tests already cover font loading through
# `generate_image`; this endpoint only needs to hand back the right file).
_FONT_BYTES = b"\x00\x01\x00\x00fake-ttf-bytes-for-testing"


@pytest.fixture(autouse=True)
async def setup_entry(hass, mock_config_entry: MockConfigEntry) -> None:
    """Set up the config entry for asset-endpoint tests."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def font_dir(hass, tmp_path: Path) -> Path:
    """Point hass's www/fonts at a real temp directory and return it."""
    fonts = tmp_path / "www" / "fonts"
    fonts.mkdir(parents=True)
    hass.config.config_dir = str(tmp_path)
    return fonts


async def test_asset_requires_auth(hass, hass_client_no_auth) -> None:
    """The endpoint is authenticated -- no auth, no asset."""
    client = await hass_client_no_auth()
    resp = await client.get(DESIGNER_ASSET_URL, params={"kind": "font", "name": "x"})
    assert resp.status == 401


async def test_asset_rejects_unsupported_kind(hass, hass_client) -> None:
    """kind=image (or anything but font) is a 400, not a silent null forever."""
    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL, params={"kind": "image", "name": "logo.png"}
    )
    assert resp.status == 400


async def test_asset_missing_font_is_404(hass, hass_client, font_dir: Path) -> None:
    """A name that resolves to no file on disk is a 404."""
    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL, params={"kind": "font", "name": "DoesNotExist"}
    )
    assert resp.status == 404


async def test_asset_rejects_path_traversal_at_the_http_layer(
    hass, hass_client, font_dir: Path
) -> None:
    """A `../` query string never reaches the view at all.

    Home Assistant's own `security_filter` middleware rejects any query
    string containing `../` outright (400) before routing -- confirms this
    endpoint gets that platform-level defense same as every other HA view,
    on top of its own guard (exercised directly, below, since the
    middleware makes a real `../` request unable to reach this view's own
    code in this test environment).
    """
    secret = font_dir.parent.parent / "secret.txt"
    secret.write_bytes(b"should never be served")

    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL,
        params={"kind": "font", "name": "../../secret.txt"},
    )
    assert resp.status == 400


def test_resolve_font_path_rejects_traversal_directly(tmp_path: Path) -> None:
    """The view's own guard, independent of HA's `security_filter`.

    `resolve()` + `relative_to()`: an escaping candidate is skipped, not
    served.
    """
    search_dir = tmp_path / "fonts"
    search_dir.mkdir()
    secret = tmp_path / "secret.ttf"
    secret.write_bytes(b"should never be served")

    # Exercises `_resolve_font_path` directly, bypassing HA's own
    # `security_filter` (which would otherwise reject this query string
    # before it ever reached the view) -- proves the function's OWN guard
    # holds regardless of that platform-level defense.
    escaping_name = str(Path("..") / "secret.ttf")
    assert _resolve_font_path([str(search_dir)], escaping_name) is None


async def test_asset_serves_a_real_font_file_bytes_for_bytes(
    hass, hass_client, font_dir: Path
) -> None:
    """The maintainer's Tinos-Bold case: drop a font in, the endpoint serves it.

    Requested by bare name (no extension) -- the same `.ttf` auto-append
    `odl_renderer.fonts.FontManager` applies when a payload references a
    font this same way, so the designer resolves to the identical file a
    real render would load.
    """
    (font_dir / "Tinos-Bold.ttf").write_bytes(_FONT_BYTES)

    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL, params={"kind": "font", "name": "Tinos-Bold"}
    )
    assert resp.status == 200
    assert resp.content_type in ("font/ttf",)
    body = await resp.read()
    assert body == _FONT_BYTES


async def test_asset_serves_a_font_named_with_its_extension(
    hass, hass_client, font_dir: Path
) -> None:
    """A name already carrying `.otf` is not double-suffixed."""
    (font_dir / "Custom.otf").write_bytes(_FONT_BYTES)

    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL, params={"kind": "font", "name": "Custom.otf"}
    )
    assert resp.status == 200
    assert resp.content_type == "font/otf"
    assert await resp.read() == _FONT_BYTES


async def test_asset_serves_an_authenticated_non_admin_user(
    hass, hass_client, hass_read_only_access_token, font_dir: Path
) -> None:
    """The asset endpoint answers every authenticated user, like the panel.

    Same contract as the render endpoint (`test_render_serves_an_
    authenticated_non_admin_user`): authorization here matches the panel's
    own visibility, and `test_asset_requires_auth` above pins that an
    unauthenticated request is still rejected.
    """
    (font_dir / "Tinos-Bold.ttf").write_bytes(_FONT_BYTES)

    client = await hass_client(hass_read_only_access_token)
    resp = await client.get(
        DESIGNER_ASSET_URL, params={"kind": "font", "name": "Tinos-Bold"}
    )
    assert resp.status == 200
    assert await resp.read() == _FONT_BYTES
