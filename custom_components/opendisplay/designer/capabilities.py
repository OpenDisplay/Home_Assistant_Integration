"""Build designer-facing device capability payloads from runtime config."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from epaper_dithering import ColorPalette, ColorScheme
from opendisplay import Rotation
from opendisplay.display_palettes import get_palette_for_display

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH

if TYPE_CHECKING:
    from .. import OpenDisplayConfigEntry


def resolve_device_id_for_entry(
    hass: HomeAssistant, entry: OpenDisplayConfigEntry
) -> str | None:
    """Resolve HA device registry id for a config entry."""
    device_registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    if devices:
        return devices[0].id

    mac = entry.unique_id
    if not mac:
        return None
    for variant in {mac, mac.upper(), mac.lower()}:
        device = device_registry.async_get_device(
            connections={(CONNECTION_BLUETOOTH, variant)}
        )
        if device is not None:
            return device.id
    return None


def build_capabilities(
    entry: OpenDisplayConfigEntry,
    device_id: str,
    *,
    user_rotate_deg: int = 0,
) -> dict[str, Any]:
    """Serialize display capabilities for the designer mount API."""
    display = entry.runtime_data.device_config.displays[0]
    cs = display.color_scheme_enum
    scheme = cs if isinstance(cs, ColorScheme) else ColorScheme.from_value(int(cs))
    palette = get_palette_for_display(display.panel_ic_type, scheme)
    colors = (
        palette.colors
        if isinstance(palette, ColorPalette)
        else palette.palette.colors
    )
    color_map: dict[str, str] = {}
    for name, rgb in colors.items():
        if isinstance(rgb, (tuple, list)) and len(rgb) >= 3:
            r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
            color_map[str(name)] = f"#{r:02x}{g:02x}{b:02x}"

    rotation = display.rotation_enum
    base = int(rotation.value if isinstance(rotation, Rotation) else rotation) % 360
    effective = (base + user_rotate_deg) % 360
    pw, ph = int(display.pixel_width), int(display.pixel_height)
    render_w, render_h = (ph, pw) if effective in (90, 270) else (pw, ph)
    accent = (
        palette.accent
        if isinstance(palette, ColorPalette)
        else scheme.accent_color
    )
    return {
        "device_id": device_id,
        "pixel_width": pw,
        "pixel_height": ph,
        "rotation_degrees": base,
        "render_width": render_w,
        "render_height": render_h,
        "color_scheme": int(scheme.value),
        "accent_color": str(accent),
        "available_colors": list(color_map),
        "color_map": color_map,
        "palette_measured": isinstance(palette, ColorPalette),
    }
