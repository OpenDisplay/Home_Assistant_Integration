"""OpenDisplay image designer -- HTTP views and sidebar panel registration."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from custom_components.opendisplay.const import DOMAIN

from .panel import (
    DESIGNER_PANEL_PATH,
    OpenDisplayDesignerStaticView,
    async_get_panel_module_url,
)
from .render import OpenDisplayDesignerRenderView

_LOGGER = logging.getLogger(__name__)
_DESIGNER_KEY = "designer"


async def async_setup_designer(hass: HomeAssistant) -> None:
    """Register designer HTTP views and the sidebar panel."""
    hass.data.setdefault(DOMAIN, {})
    designer_data = hass.data[DOMAIN].setdefault(_DESIGNER_KEY, {})

    if not designer_data.get("views_registered"):
        hass.http.register_view(OpenDisplayDesignerRenderView(hass))
        hass.http.register_view(OpenDisplayDesignerStaticView(hass))
        designer_data["views_registered"] = True

    if designer_data.get("panel_registered"):
        return

    try:
        from homeassistant.components import panel_custom

        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=DESIGNER_PANEL_PATH,
            webcomponent_name="opendisplay-designer-panel",
            sidebar_title="OpenDisplay Designer",
            sidebar_icon="mdi:monitor-edit",
            module_url=await async_get_panel_module_url(hass),
            require_admin=False,
        )
        designer_data["panel_registered"] = True
        _LOGGER.info("OpenDisplay designer panel registered")
    except (AttributeError, ImportError, RuntimeError, ValueError) as err:
        _LOGGER.warning("Failed to register OpenDisplay designer panel: %s", err)
