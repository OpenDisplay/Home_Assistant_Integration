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

Images: this integration has no font-independent image search path (no
`_image_search_dirs` equivalent -- `generate_image`'s own image references
resolve through media-source/URL handling in services.py, not a bare-name
directory search the way fonts do). Font-only is therefore the honest v1
here; `kind=image` is rejected with a clear 400 rather than silently
answering `None` forever (indistinguishable from "not found" to the
designer). Revisit once/if an equivalent image search path exists.
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from custom_components.opendisplay.services import _font_search_dirs

DESIGNER_ASSET_URL = "/api/opendisplay/designer/asset"

_ALLOWED_KINDS = ("font",)


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


class OpenDisplayDesignerAssetView(HomeAssistantView):
    """Resolve a font asset by name for the designer's `resolveAsset` seam."""

    url = DESIGNER_ASSET_URL
    name = "opendisplay:designer_asset"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Serve one font file by name, or 404/400."""
        kind = request.query.get("kind", "")
        name = request.query.get("name", "")
        if kind not in _ALLOWED_KINDS:
            return web.json_response(
                {"message": f"unsupported kind: {kind!r} (only 'font' is resolvable)"},
                status=400,
            )
        if not name:
            return web.Response(status=404, text="Not found")

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
