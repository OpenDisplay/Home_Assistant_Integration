"""Serve the small frontend module that adds a QR-scan button to key fields.

The module (``frontend/qr-scan.js``) only enhances the encryption-key text
field of OpenDisplay config flows when the page runs inside a Home Assistant
companion app that reports ``hasBarCodeScanner``; it uses the app's native
scanner over the external bus (``bar_code/scan``). Everywhere else the field
stays a plain text box that also accepts a pasted QR link.

Registration is best-effort: ``add_extra_js_url`` modules are injected into
``index.html``, so a page (or app webview) opened before the first registration
only picks the button up after it reloads.
"""

import json
import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_JS_FILE = Path(__file__).parent / "frontend" / "qr-scan.js"
_URL_BASE = f"/{DOMAIN}_static"
_JS_URL = f"{_URL_BASE}/qr-scan.js"
_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_frontend_registered")


def _version() -> str:
    manifest = json.loads((Path(__file__).parent / "manifest.json").read_text())
    return str(manifest.get("version", "0"))


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Register the static path and the extra module URL once per HA run."""
    if hass.data.get(_REGISTERED):
        return
    if "frontend" not in hass.config.components or "http" not in hass.config.components:
        # Headless/test instances: nothing to inject into.
        return
    hass.data[_REGISTERED] = True

    # Imported lazily so the integration never hard-depends on frontend.
    from homeassistant.components.frontend import add_extra_js_url

    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_JS_URL, str(_JS_FILE), cache_headers=False)]
        )
    except RuntimeError:
        # Path already registered (e.g. integration reloaded) -- fine.
        _LOGGER.debug("QR scan module path already registered")
    version = await hass.async_add_executor_job(_version)
    add_extra_js_url(hass, f"{_JS_URL}?v={version}")
