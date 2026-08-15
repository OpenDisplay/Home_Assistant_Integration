"""Designer hooks for the OpenDisplay image entity (capabilities + placeholder)."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Any

from PIL import Image as PILImage

import homeassistant.util.dt as dt_util

from .capabilities import build_capabilities, resolve_device_id_for_entry

if TYPE_CHECKING:
    from .. import OpenDisplayConfigEntry

_LOGGER = logging.getLogger(__name__)


def _blank_white_jpeg(width: int, height: int) -> bytes:
    img = PILImage.new(
        "RGB",
        (max(1, min(width, 4096)), max(1, min(height, 4096))),
        (255, 255, 255),
    )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _refresh_capability_attributes(
    entity: Any, entry: OpenDisplayConfigEntry
) -> dict[str, Any]:
    try:
        device_id = ""
        registry_entry = getattr(entity, "registry_entry", None)
        if registry_entry is not None and registry_entry.device_id:
            device_id = registry_entry.device_id
        else:
            device_id = resolve_device_id_for_entry(entity.hass, entry) or ""
        return build_capabilities(entry, device_id)
    except Exception:
        _LOGGER.exception(
            "Failed to build designer capabilities for %s",
            getattr(entity, "entity_id", entity.unique_id),
        )
        return {}


async def designer_on_entity_added(
    entity: Any, entry: OpenDisplayConfigEntry | None
) -> None:
    """Publish capability attrs and a white placeholder JPEG for the designer."""
    if entry is None:
        return
    entity._designer_capability_attributes = _refresh_capability_attributes(
        entity, entry
    )
    if entity._image_bytes:
        entity.async_write_ha_state()
        return
    try:
        display = entry.runtime_data.device_config.displays[0]
        width, height = int(display.pixel_width), int(display.pixel_height)
        if width <= 0 or height <= 0:
            width, height = 296, 128
    except (AttributeError, IndexError, TypeError, ValueError):
        width, height = 296, 128
    try:
        entity._image_bytes = await entity.hass.async_add_executor_job(
            _blank_white_jpeg, width, height
        )
        entity._attr_image_last_updated = dt_util.utcnow()
    except Exception:
        _LOGGER.exception(
            "Failed to create placeholder image for %s", entity.entity_id
        )
    entity.async_write_ha_state()


def designer_extra_state_attributes(
    entity: Any, entry: OpenDisplayConfigEntry | None
) -> dict[str, Any]:
    if entry is None:
        return {}
    attrs = getattr(entity, "_designer_capability_attributes", None)
    if not attrs:
        attrs = _refresh_capability_attributes(entity, entry)
        entity._designer_capability_attributes = attrs
    return attrs
