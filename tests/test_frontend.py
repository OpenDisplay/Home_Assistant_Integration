"""Test registration of the QR-scan frontend module."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.opendisplay.frontend import _JS_FILE, async_register_frontend


def test_module_file_is_packaged() -> None:
    """The JS module ships inside the integration directory."""
    assert _JS_FILE.is_file()


async def test_noop_without_frontend(hass: HomeAssistant) -> None:
    """Nothing is registered on an instance without the frontend."""
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js:
        await async_register_frontend(hass)
    add_js.assert_not_called()


async def test_registers_once(hass: HomeAssistant) -> None:
    """Static path + extra module URL are registered exactly once."""
    hass.config.components.update({"frontend", "http"})
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js:
        await async_register_frontend(hass)
        await async_register_frontend(hass)

    hass.http.async_register_static_paths.assert_awaited_once()
    (config,) = hass.http.async_register_static_paths.await_args.args[0]
    assert config.url_path == "/opendisplay_static/qr-scan.js"
    assert config.path == str(_JS_FILE)
    add_js.assert_called_once()
    url = add_js.call_args.args[1]
    assert url.startswith("/opendisplay_static/qr-scan.js?v=")
