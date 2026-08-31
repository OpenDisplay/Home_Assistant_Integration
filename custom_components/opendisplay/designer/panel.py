"""HTTP views and path constants for the OpenDisplay designer panel."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from custom_components.opendisplay.const import DOMAIN

DESIGNER_PANEL_PATH = "opendisplay-designer"
DESIGNER_STATIC_URL = f"/api/{DOMAIN}/designer/static"

FRONTEND_DIR = Path(__file__).parent / "frontend"
PANEL_JS_REL = "panel/opendisplay-designer-panel.js"
# designer 3.x host contract (targets/actions/status seams; render endpoint
# preview; WYSIWYG send reading the live action context). Bump this whenever
# the panel JS changes in a way that isn't already covered by its own mtime
# (get_frontend_cache_token appends that too) -- e.g. a vendor library bump
# that doesn't touch the panel JS's own file.
DESIGNER_FRONTEND_BUILD = "20260831c"


def get_frontend_cache_token() -> str:
    """Return a cache-busting token for the panel JS's static URL."""
    panel_js = FRONTEND_DIR / PANEL_JS_REL
    try:
        return f"{DESIGNER_FRONTEND_BUILD}-{panel_js.stat().st_mtime_ns}"
    except OSError:
        return DESIGNER_FRONTEND_BUILD


def get_panel_module_url() -> str:
    """Return the panel JS's static URL, cache-busted."""
    return f"{DESIGNER_STATIC_URL}/{PANEL_JS_REL}?v={get_frontend_cache_token()}"


async def async_get_panel_module_url(hass: HomeAssistant) -> str:
    """Return the panel JS's static URL, computed off the event loop."""
    return await hass.async_add_executor_job(get_panel_module_url)


def _resolve_static_path(path: str) -> Path:
    candidate = (FRONTEND_DIR / Path(path)).resolve()
    candidate.relative_to(FRONTEND_DIR.resolve())
    return candidate


# The panel JS's own bare `import ... from '../vendor/...'` specifiers carry
# no query string at all -- a browser's ES module resolver does not inherit
# one from the importING module's URL onto a relative import, so panel.js's
# own `?v=` (from get_panel_module_url) never reaches its vendor imports,
# and the ~5.6MB designer bundle they pull in gets the strict no-cache
# headers below on every load instead of the long-cache treatment a
# `?v=`-tokened URL gets. Rewritten here, at serve time, into the exact
# same cache-busting query panel.js's own URL carries -- cheap (one string
# substitution on an already-read, already-small file) and correct (the
# token changes whenever get_frontend_cache_token()'s inputs do, so a
# vendor bump invalidates this the same way editing panel.js itself does).
_VENDOR_IMPORT_TARGETS = (
    "../vendor/odl-drawcustom-designer.js",
    "../vendor/js-yaml.mjs",
)


def _versioned_vendor_imports(data: bytes) -> bytes:
    """Append the current cache-busting token to panel.js's own vendor imports."""
    token = get_frontend_cache_token()
    text = data.decode("utf-8")
    for target in _VENDOR_IMPORT_TARGETS:
        text = text.replace(f"'{target}'", f"'{target}?v={token}'")
    return text.encode("utf-8")


class OpenDisplayDesignerStaticView(HomeAssistantView):
    """Serve designer panel JS and vendor assets."""

    url = f"{DESIGNER_STATIC_URL}/{{path:.*}}"
    name = "opendisplay:designer_static"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(self, request: web.Request, path: str) -> web.Response:
        """Serve one static file from designer/frontend/."""
        if ".." in path or path.startswith("/"):
            return web.Response(status=403, text="Forbidden")
        try:
            file_path = await self.hass.async_add_executor_job(
                _resolve_static_path, path
            )
        except ValueError:
            return web.Response(status=403, text="Forbidden")
        if not file_path.is_file():
            return web.Response(status=404, text="Not found")
        try:
            data = await self.hass.async_add_executor_job(file_path.read_bytes)
        except OSError:
            return web.Response(status=500, text="Error")

        if path == PANEL_JS_REL:
            data = await self.hass.async_add_executor_job(
                _versioned_vendor_imports, data
            )

        ctype, _ = mimetypes.guess_type(str(file_path))
        if path.endswith((".js", ".mjs")):
            ctype = "application/javascript"
        elif path.endswith(".css"):
            ctype = "text/css"
        ctype = ctype or "application/octet-stream"
        # A request carrying `?v=<token>` (get_panel_module_url's own
        # cache-busting query) names a URL that only this exact content will
        # ever answer -- safe to cache aggressively. A bare path (no token --
        # e.g. the panel JS's own `../vendor/...` relative imports, which
        # carry no query string at all) gets the strict no-cache headers
        # instead, since the same URL can start answering different bytes
        # after a vendor bump or a panel edit.
        if "v" in request.query:
            headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        else:
            headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        if ctype in ("application/javascript", "text/css"):
            return web.Response(
                body=data, content_type=ctype, charset="utf-8", headers=headers
            )
        return web.Response(body=data, content_type=ctype, headers=headers)
