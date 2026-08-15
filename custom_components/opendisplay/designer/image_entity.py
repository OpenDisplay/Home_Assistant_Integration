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

_DEFAULT_PLACEHOLDER_SIZE = (296, 128)


def _blank_white_jpeg(width: int, height: int) -> bytes:
    w = max(1, min(int(width), 4096))
    h = max(1, min(int(height), 4096))
    img = PILImage.new("RGB", (w, h), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _placeholder_size(entry: OpenDisplayConfigEntry) -> tuple[int, int]:
    try:
        display = entry.runtime_data.device_config.displays[0]
        w = int(display.pixel_width)
        h = int(display.pixel_height)
        if w > 0 and h > 0:
            return w, h
    except (AttributeError, IndexError, TypeError, ValueError):
        pass
    return _DEFAULT_PLACEHOLDER_SIZE


def _capability_device_id(entity: Any, entry: OpenDisplayConfigEntry) -> str:
    registry_entry = getattr(entity, "registry_entry", None)
    if registry_entry is not None and registry_entry.device_id:
        return registry_entry.device_id
    return resolve_device_id_for_entry(entity.hass, entry) or ""


def _refresh_capability_attributes(
    entity: Any, entry: OpenDisplayConfigEntry
) -> dict[str, Any]:
    try:
        return build_capabilities(
            entry,
            _capability_device_id(entity, entry),
            user_rotate_deg=0,
        )
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
    width, height = _placeholder_size(entry)
    try:
        entity._image_bytes = await entity.hass.async_add_executor_job(
            _blank_white_jpeg, width, height
        )
    except Exception:
        _LOGGER.exception(
            "Failed to create placeholder image for %s", entity.entity_id
        )
        entity.async_write_ha_state()
        return
    entity._attr_image_last_updated = dt_util.utcnow()
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
