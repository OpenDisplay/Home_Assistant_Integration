"""Tests for the designer's asset-resolution endpoint (issue #138 last tier).

Maintainer ruling (tier-2, real hardware): "if the server renderer can use
it, the client must get it mapped". Auth, 404, path-traversal and
content-type are exercised directly against the HTTP view; the live-harness
check (drop a real font file into a search dir, confirm the endpoint serves
the exact same bytes `_font_search_dirs`/`FontManager` would load for a
canvas render -- the maintainer's Tinos-Bold case) is covered by
`test_asset_serves_a_real_font_file_bytes_for_bytes`, run against a real
temp directory rather than mocked I/O.

`kind=image` (tier-2 round 3, real hardware: a display's payload referenced
`/media/pohl89-480h.png`, the server render resolved it, the designer
preview showed it missing) is the second half. It carries a permitted-root
policy the font half does not need, because the reference is a caller-
supplied ABSOLUTE PATH rather than a bare name resolved against directories
this integration chose -- so the image tests below are as much about what
the endpoint must REFUSE (traversal, symlink escapes, paths outside the
roots, non-image files, `http(s)://` sources) as about what it serves.
"""

from pathlib import Path

from PIL import Image as PILImage
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opendisplay.designer.asset import (
    DESIGNER_ASSET_URL,
    _resolve_font_path,
    _resolve_image_path,
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
    """A kind outside `AssetKind` is a 400, not a silent null forever."""
    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL, params={"kind": "video", "name": "clip.mp4"}
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


# --- kind=image -------------------------------------------------------------
#
# The image half of the endpoint. Unlike fonts (a bare name resolved against
# directories this integration picks), an image reference is whatever
# absolute path the payload carries, so every test below that asserts a
# REFUSAL is load-bearing: this view hands raw file bytes to any
# authenticated user, admin or not (`docs/designer.md`, "Access and
# exposure").


def _png_bytes(path: Path, color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    """Write a real, decodable 4x4 PNG at `path` and return its bytes."""
    PILImage.new("RGB", (4, 4), color).save(path, format="PNG")
    return path.read_bytes()


@pytest.fixture
def media_dir(hass, tmp_path: Path) -> Path:
    """Make a real temp directory the one permitted image root."""
    media = tmp_path / "media"
    media.mkdir(parents=True)
    hass.config.allowlist_external_dirs = {str(media)}
    return media


async def test_asset_serves_an_image_inside_a_permitted_root(
    hass, hass_client, media_dir: Path
) -> None:
    """The maintainer's `/media/pohl89-480h.png` case, byte for byte.

    Red-first: before `kind=image` existed, this was the 400 that
    `test_asset_rejects_unsupported_kind` used to pin.
    """
    expected = _png_bytes(media_dir / "pohl89-480h.png")

    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL,
        params={"kind": "image", "name": str(media_dir / "pohl89-480h.png")},
    )
    assert resp.status == 200, await resp.text()
    assert resp.content_type == "image/png"
    assert await resp.read() == expected


async def test_asset_serves_an_image_from_a_subdirectory_of_a_root(
    hass, hass_client, media_dir: Path
) -> None:
    """Containment is "under the root", not "directly in it"."""
    nested = media_dir / "panels" / "kitchen"
    nested.mkdir(parents=True)
    expected = _png_bytes(nested / "logo.png")

    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL, params={"kind": "image", "name": str(nested / "logo.png")}
    )
    assert resp.status == 200, await resp.text()
    assert await resp.read() == expected


async def test_asset_rejects_image_path_traversal_at_the_http_layer(
    hass, hass_client, media_dir: Path
) -> None:
    """A `../` query string never reaches the view at all.

    Home Assistant's own `security_filter` middleware rejects any query
    string containing `../` outright, before routing -- the same
    platform-level defense the font half gets, on top of this view's own
    guard (exercised directly below, since the middleware makes a real
    `../` request unable to reach the view's code in this environment).
    """
    secret = media_dir.parent / "secret.png"
    _png_bytes(secret)

    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL,
        params={"kind": "image", "name": f"{media_dir}/../secret.png"},
    )
    assert resp.status == 400


def test_resolve_image_path_rejects_traversal_directly(tmp_path: Path) -> None:
    """The view's own guard, independent of HA's `security_filter`.

    Containment is checked AFTER `resolve()`, so `..` is collapsed before
    the comparison rather than pattern-matched before it.
    """
    root = tmp_path / "media"
    root.mkdir()
    secret = tmp_path / "secret.png"
    _png_bytes(secret)

    assert _resolve_image_path([str(root)], f"{root}/../secret.png") is None


def test_resolve_image_path_rejects_a_symlink_leaving_the_roots(
    tmp_path: Path,
) -> None:
    """A symlink inside a root pointing outside it resolves outside, and is refused.

    This is why containment is re-checked after resolution and not before:
    the pre-resolution path (`<root>/escape.png`) is textually contained.
    """
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside.png"
    _png_bytes(outside)
    (root / "escape.png").symlink_to(outside)

    assert (root / "escape.png").is_file()  # the lure really does resolve
    assert _resolve_image_path([str(root)], str(root / "escape.png")) is None


async def test_asset_rejects_a_symlink_leaving_the_roots_over_http(
    hass, hass_client, media_dir: Path
) -> None:
    """The symlink refusal, end to end, as a token holder would attempt it."""
    outside = media_dir.parent / "outside.png"
    _png_bytes(outside)
    (media_dir / "escape.png").symlink_to(outside)

    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL,
        params={"kind": "image", "name": str(media_dir / "escape.png")},
    )
    assert resp.status == 404


async def test_asset_rejects_an_image_outside_the_permitted_roots(
    hass, hass_client, media_dir: Path, tmp_path: Path
) -> None:
    """A perfectly valid image the renderer would happily load is still refused.

    The renderer accepts ANY absolute path (`odl_renderer.media_loader.
    _load_from_file`). This endpoint deliberately does not: it hands bytes
    to a browser, so it serves only what Home Assistant itself already
    exposes (`hass.config.allowlist_external_dirs`).
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _png_bytes(elsewhere / "private.png")

    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL,
        params={"kind": "image", "name": str(elsewhere / "private.png")},
    )
    assert resp.status == 404


async def test_asset_does_not_proxy_http_image_sources(
    hass, hass_client, media_dir: Path
) -> None:
    """`http(s)://` is refused outright -- no server-side fetch from this view.

    The render path fetches remote sources server-side; that is a
    pre-existing property of the SERVICE and is deliberately not widened
    into a designer-side fetch-anything surface.
    """
    client = await hass_client()
    for source in (
        "http://169.254.169.254/latest/meta-data/",
        "https://example.invalid/a.png",
    ):
        resp = await client.get(
            DESIGNER_ASSET_URL, params={"kind": "image", "name": source}
        )
        assert resp.status == 400, source


async def test_asset_rejects_a_relative_image_name(
    hass, hass_client, media_dir: Path
) -> None:
    """No bare-name search path exists for images -- unlike fonts.

    Inventing one here would resolve a reference the renderer itself
    cannot, which is the same mismatch in the other direction.
    """
    _png_bytes(media_dir / "logo.png")

    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL, params={"kind": "image", "name": "logo.png"}
    )
    assert resp.status == 400


async def test_asset_refuses_a_non_image_file_inside_a_permitted_root(
    hass, hass_client, media_dir: Path
) -> None:
    """The endpoint is an IMAGE resolver, not a file-read oracle.

    Media directories hold more than images. Anything PIL cannot identify
    as an image is refused, so a token holder cannot read arbitrary files
    that happen to sit under a permitted root.
    """
    (media_dir / "secrets.yaml").write_text("api_key: hunter2\n")

    client = await hass_client()
    resp = await client.get(
        DESIGNER_ASSET_URL,
        params={"kind": "image", "name": str(media_dir / "secrets.yaml")},
    )
    assert resp.status == 404
    assert b"hunter2" not in await resp.read()


async def test_asset_serves_an_image_to_an_authenticated_non_admin_user(
    hass, hass_client, hass_read_only_access_token, media_dir: Path
) -> None:
    """Same contract as fonts and as the render endpoint: authenticated, not admin."""
    expected = _png_bytes(media_dir / "logo.png")

    client = await hass_client(hass_read_only_access_token)
    resp = await client.get(
        DESIGNER_ASSET_URL,
        params={"kind": "image", "name": str(media_dir / "logo.png")},
    )
    assert resp.status == 200
    assert await resp.read() == expected


async def test_asset_image_requires_auth(
    hass, hass_client_no_auth, media_dir: Path
) -> None:
    """No auth, no image bytes."""
    _png_bytes(media_dir / "logo.png")

    client = await hass_client_no_auth()
    resp = await client.get(
        DESIGNER_ASSET_URL,
        params={"kind": "image", "name": str(media_dir / "logo.png")},
    )
    assert resp.status == 401
