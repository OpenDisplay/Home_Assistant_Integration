"""OpenDisplay image designer -- HTTP views (render endpoint, panel to follow)."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from custom_components.opendisplay.const import DOMAIN

from .render import OpenDisplayDesignerRenderView

_LOGGER = logging.getLogger(__name__)
_DESIGNER_KEY = "designer"


async def async_setup_designer(hass: HomeAssistant) -> None:
    """Register the designer's HTTP views."""
    hass.data.setdefault(DOMAIN, {})
    designer_data = hass.data[DOMAIN].setdefault(_DESIGNER_KEY, {})

    if designer_data.get("views_registered"):
        return
    hass.http.register_view(OpenDisplayDesignerRenderView(hass))
    designer_data["views_registered"] = True
