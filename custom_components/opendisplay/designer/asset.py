"""Authenticated HTTP view resolving assets for the designer's `resolveAsset` seam.

Maintainer ruling (tier-2, real hardware): "if the server renderer can use
it, the client must get it mapped" -- the resolveAsset gap `docs/designer.md`
previously only documented is promoted from documented-gap to build item
here.

Implements the LAST tier of the designer's asset resolution (issue #138,
ADR-002 amendment; `HostAssetResolver` in the vendored `.d.ts`): a payload
may reference a font by bare name (`Tinos-Bold`, `Tinos-Bold.ttf`) the same
way a hand-written `drawcustom` payload does, resolved against this
integration's own font search directories
(`custom_components.opendisplay.services._font_search_dirs` --
`www/fonts`, `media/fonts`, `/media/fonts`) so a font the SEND/RENDER path
can load is the SAME file the designer's own canvas preview gets, never a
font the server renders with but the designer substitutes or errors on.

Images (tier-2 round 3, real hardware: a display's payload referenced
`/media/pohl89-480h.png`, the server render resolved it, the designer
preview showed the image missing) work differently, because the renderer
itself treats them differently: `odl_renderer.media_loader.load_image`
takes an ABSOLUTE PATH and opens it directly -- there is no bare-name
search path for images the way `FontManager` has one for fonts. So
`kind=image` resolves the caller's own absolute path, and the reference
that reaches this view is whatever the payload carries.

That difference is the whole reason the image half needs a path policy the
font half does not. This view returns raw file bytes to ANY authenticated
user, admin or not (`docs/designer.md`, "Access and exposure"), so it is
deliberately STRICTER than the renderer:

* **Permitted roots** are `hass.config.allowlist_external_dirs` -- Home
  Assistant's own canonical answer to "which local directories may be read
  on a user's behalf". Core composes that set as `{<config>/www} |
  set(hass.config.media_dirs.values()) | <admin's own
  allowlist_external_dirs>` (`homeassistant/core_config.py`), which on a
  Home Assistant OS install is exactly `/config/www` and `/media` -- the
  maintainer's own path. Nothing is invented here: an operator widens or
  narrows what the designer can read with the same `configuration.yaml`
  key that governs every other local-file feature.
* **Containment is re-checked AFTER `resolve()`**, so `..` segments are
  collapsed and symlinks followed before the comparison -- a symlink
  inside a permitted root pointing outside it is refused, even though its
  pre-resolution path is textually contained.
* **`http(s)://` is refused outright.** The render path does fetch remote
  sources server-side; that is a pre-existing property of the service and
  is deliberately not widened into a designer-side fetch-anything surface.
* **Only files PIL can identify as images are served**, and the response
  content type is PIL's own for the identified format. Media directories
  hold more than images; without this the endpoint would be a file-read
  oracle for everything under a permitted root.

Net effect for a token-holding non-admin: they can read image files under
the directories Home Assistant already exposes, and nothing else -- no
arbitrary path, no non-image file, no network fetch, and no existence
oracle (everything refused for a path reason answers the same 404 as a
missing file).

The consequence to be honest about: the renderer accepts absolute paths
this endpoint refuses, so an image outside the permitted roots renders on
send but shows the designer's explicit missing-asset state in preview.
That direction is the safe one, and it is documented in
`docs/designer.md`.
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from PIL import Image as PILImage

from custom_components.opendisplay.services import _font_search_dirs

DESIGNER_ASSET_URL = "/api/opendisplay/designer/asset"

# `AssetKind` in the vendored `odl-drawcustom-designer.d.ts`, in full.
_ALLOWED_KINDS = ("font", "image")

# Bounds one request's read into memory. A drawcustom image is destined for
# a panel measured in hundreds of pixels; 32 MiB is far above any real one
# and far below "an authenticated user can make Home Assistant read an
# arbitrarily large file into RAM on demand". A file over the cap is
# refused like any other unresolvable reference -- the renderer would still
# load it on send, same as it loads paths outside the permitted roots.
_MAX_IMAGE_BYTES = 32 * 1024 * 1024


def _permitted_image_roots(hass: HomeAssistant) -> list[str]:
    """Return the directories an image may be served from.

    Home Assistant's own allowlist, not a policy this integration invents --
    see the module docstring.
    """
    return sorted(hass.config.allowlist_external_dirs)


def _resolve_image_path(permitted_roots: list[str], name: str) -> Path | None:
    """Return `name` as a real file under one of `permitted_roots`, else None.

    `name` is the payload's own image reference, which for a local file is
    an absolute path (`odl_renderer.media_loader.load_image`'s own rule:
    HTTP(S) first, then `data:`, then a leading `/`). Anything else -- a
    relative name, a `data:` URI, a URL -- has no local file behind it and
    is not this function's business.

    Containment is checked after `resolve()` on BOTH sides: the candidate,
    so `..` is collapsed and symlinks are followed before comparing, and
    each root, so a permitted directory reached through a symlink (a
    container bind-mount layout) still matches its own real files.
    """
    if not name.startswith("/"):
        return None

    try:
        candidate = Path(name).resolve()
    except OSError:
        return None

    for root in permitted_roots:
        try:
            root_path = Path(root).resolve()
        except OSError:
            continue
        try:
            candidate.relative_to(root_path)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
        return None
    return None


def _read_image_asset(
    permitted_roots: list[str], name: str
) -> tuple[bytes, str] | None:
    """Return (bytes, content type) for a permitted image file, else None.

    Identification is PIL's, not the file extension's: the endpoint serves
    what the renderer could actually decode, and refuses everything else so
    it cannot be used to read non-image files out of a media directory.
    Runs in an executor -- every file operation here is blocking.
    """
    path = _resolve_image_path(permitted_roots, name)
    if path is None:
        return None

    try:
        if path.stat().st_size > _MAX_IMAGE_BYTES:
            return None
        with PILImage.open(path) as img:
            image_format = img.format
    except OSError:
        return None
    except Exception:
        return None

    if not image_format:
        return None
    PILImage.init()  # populate PILImage.MIME for every registered plugin
    content_type = PILImage.MIME.get(image_format)
    if content_type is None:
        return None

    try:
        return path.read_bytes(), content_type
    except OSError:
        return None


def _resolve_font_path(search_dirs: list[str], name: str) -> Path | None:
    """Resolve `name` within `search_dirs`, guarded against path traversal.

    Mirrors `odl_renderer.fonts.FontManager`'s own name -> file resolution
    (a bare name gets `.ttf` appended unless it already ends in `.ttf`/
    `.otf`) so a name the designer asks for resolves to the exact same file
    the render/send pipeline would load for that same payload reference --
    never a different file behind the same name (issue #138's `(kind, name)`
    contract). Guarded exactly like `OpenDisplayDesignerStaticView`
    (`panel.py`'s `_resolve_static_path`): resolve the candidate, then
    require it stay under the search directory it came from -- a `name` that
    escapes via `../` is skipped (falls through to the next search dir, then
    to a 404), never a 500 or a path outside the intended tree.
    """
    font_name = name if name.endswith((".ttf", ".otf")) else f"{name}.ttf"
    for directory in search_dirs:
        root = Path(directory).resolve()
        candidate = (root / font_name).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


_CONTENT_TYPES = {".ttf": "font/ttf", ".otf": "font/otf"}


# Every refusal that is about a PATH answers the same 404 a genuinely
# missing file does -- "outside the permitted roots", "symlink escapes
# them", "not an image", "too large" and "no such file" are indistinguish-
# able to the caller, so the endpoint is not an existence oracle for the
# filesystem outside what it is willing to serve.
_NOT_FOUND = "Not found"


class OpenDisplayDesignerAssetView(HomeAssistantView):
    """Resolve a font or image asset for the designer's `resolveAsset` seam."""

    url = DESIGNER_ASSET_URL
    name = "opendisplay:designer_asset"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Serve one font or image file by name, or 404/400."""
        kind = request.query.get("kind", "")
        name = request.query.get("name", "")
        if kind not in _ALLOWED_KINDS:
            return web.json_response(
                {
                    "message": f"unsupported kind: {kind!r} "
                    f"(resolvable: {', '.join(_ALLOWED_KINDS)})"
                },
                status=400,
            )
        if not name:
            return web.Response(status=404, text=_NOT_FOUND)

        if kind == "image":
            return await self._get_image(name)

        search_dirs = await self.hass.async_add_executor_job(
            _font_search_dirs, self.hass
        )
        font_path = await self.hass.async_add_executor_job(
            _resolve_font_path, search_dirs, name
        )
        if font_path is None:
            return web.Response(status=404, text="Not found")

        try:
            data = await self.hass.async_add_executor_job(font_path.read_bytes)
        except OSError:
            return web.Response(status=500, text="Error")

        content_type = _CONTENT_TYPES.get(font_path.suffix, "application/octet-stream")
        # Keyed only by name (no cache-busting token like the static view's
        # `?v=`) and font files in these directories can change without this
        # integration knowing (a user replacing a font on disk) -- no-cache
        # rather than immutable/long-cache, so a swapped file is picked up on
        # the next request instead of serving stale bytes for up to a year.
        # Justified since fonts aren't re-fetched on every render (the
        # designer resolves and caches an asset once per session, per
        # issue #138's own contract) -- the cost of skipping aggressive
        # caching here is low.
        return web.Response(
            body=data,
            content_type=content_type,
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    async def _get_image(self, name: str) -> web.Response:
        """Serve one image file by absolute path, from a permitted root."""
        if name.startswith(("http://", "https://")):
            # Refused, not proxied -- see the module docstring. The render
            # path's own server-side fetch of remote sources is a property
            # of that service, not something this view extends to the
            # browser.
            return web.json_response(
                {"message": "remote image sources are not resolved by this endpoint"},
                status=400,
            )
        if not name.startswith("/"):
            return web.json_response(
                {
                    "message": "an image must be referenced by absolute path "
                    "(there is no bare-name image search path)"
                },
                status=400,
            )

        roots = await self.hass.async_add_executor_job(
            _permitted_image_roots, self.hass
        )
        resolved = await self.hass.async_add_executor_job(
            _read_image_asset, roots, name
        )
        if resolved is None:
            return web.Response(status=404, text=_NOT_FOUND)

        data, content_type = resolved
        # Same reasoning as the font branch: keyed only by path, and the
        # file behind it can change without this integration knowing.
        return web.Response(
            body=data,
            content_type=content_type,
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )
