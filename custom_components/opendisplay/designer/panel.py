"""HTTP views and path constants for the OpenDisplay designer panel."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from ..const import DOMAIN

DESIGNER_PANEL_PATH = "opendisplay-designer"
DESIGNER_STATIC_URL = f"/api/{DOMAIN}/designer/static"

FRONTEND_DIR = Path(__file__).parent / "frontend"
PANEL_JS_REL = "panel/opendisplay-designer-panel.js"
DESIGNER_FRONTEND_BUILD = "20260815h"


def get_frontend_cache_token() -> str:
    panel_js = FRONTEND_DIR / PANEL_JS_REL
    try:
        return f"{DESIGNER_FRONTEND_BUILD}-{panel_js.stat().st_mtime_ns}"
    except OSError:
        return DESIGNER_FRONTEND_BUILD


def get_panel_module_url() -> str:
    return f"{DESIGNER_STATIC_URL}/{PANEL_JS_REL}?v={get_frontend_cache_token()}"


async def async_get_panel_module_url(hass: HomeAssistant) -> str:
    return await hass.async_add_executor_job(get_panel_module_url)


def _resolve_static_path(path: str) -> Path:
    candidate = (FRONTEND_DIR / Path(path)).resolve()
    candidate.relative_to(FRONTEND_DIR.resolve())
    return candidate


class OpenDisplayDesignerStaticView(HomeAssistantView):
    """Serve designer panel JS and vendor assets."""

    url = f"{DESIGNER_STATIC_URL}/{{path:.*}}"
    name = "opendisplay:designer_static"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, path: str) -> web.Response:
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

        ctype, _ = mimetypes.guess_type(str(file_path))
        if path.endswith((".js", ".mjs")):
            ctype = "application/javascript"
        elif path.endswith(".css"):
            ctype = "text/css"
        ctype = ctype or "application/octet-stream"
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
