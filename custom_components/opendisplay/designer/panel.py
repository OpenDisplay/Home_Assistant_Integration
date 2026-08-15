"""HTTP views: static frontend for the OpenDisplay designer panel."""

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DESIGNER_STATIC_URL

FRONTEND_DIR = Path(__file__).parent / "frontend"
PANEL_JS_REL = "panel/opendisplay-designer-panel.js"
# Bump when shipping frontend fixes so HA panel cache invalidates.
DESIGNER_FRONTEND_BUILD = "20260815a"


def get_frontend_cache_token() -> str:
    panel_js = FRONTEND_DIR / PANEL_JS_REL
    try:
        return f"{DESIGNER_FRONTEND_BUILD}-{panel_js.stat().st_mtime_ns}"
    except OSError:
        return DESIGNER_FRONTEND_BUILD


def _append_cache_token(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}v={get_frontend_cache_token()}"


def get_panel_module_url() -> str:
    return _append_cache_token(f"{DESIGNER_STATIC_URL}/{PANEL_JS_REL}")


async def _run_in_executor(
    hass: HomeAssistant, func: Callable[..., Any], *args: Any
) -> Any:
    if hasattr(hass, "async_add_executor_job"):
        return await hass.async_add_executor_job(func, *args)
    return await asyncio.to_thread(func, *args)


async def async_get_panel_module_url(hass: HomeAssistant) -> str:
    return await _run_in_executor(hass, get_panel_module_url)


def _resolve_static_path(path: str) -> Path:
    rel = Path(path)
    candidate = (FRONTEND_DIR / rel).resolve()
    candidate.relative_to(FRONTEND_DIR.resolve())
    return candidate


def _no_cache_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }


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
            file_path = await _run_in_executor(self.hass, _resolve_static_path, path)
        except ValueError:
            return web.Response(status=403, text="Forbidden")
        if not file_path.is_file():
            return web.Response(status=404, text="Not found")
        try:
            data = await _run_in_executor(self.hass, file_path.read_bytes)
        except OSError:
            return web.Response(status=500, text="Error")
        ctype, _ = mimetypes.guess_type(str(file_path))
        if path.endswith((".js", ".mjs")):
            ctype = "application/javascript"
        elif path.endswith(".css"):
            ctype = "text/css"
        if not ctype:
            ctype = "application/octet-stream"

        charset = (
            "utf-8" if ctype in ("application/javascript", "text/css") else None
        )
        if charset:
            return web.Response(
                body=data,
                content_type=ctype,
                charset=charset,
                headers=_no_cache_headers(),
            )
        return web.Response(
            body=data,
            content_type=ctype,
            headers=_no_cache_headers(),
        )
