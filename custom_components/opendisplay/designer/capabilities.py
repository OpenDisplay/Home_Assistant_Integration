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


def _rotation_degrees(rotation: Rotation | int) -> int:
    if isinstance(rotation, Rotation):
        return int(rotation.value)
    return int(rotation) % 360


def _color_scheme_enum(display: Any) -> ColorScheme:
    cs = display.color_scheme_enum
    if isinstance(cs, ColorScheme):
        return cs
    return ColorScheme.from_value(int(cs))


def _palette_for_display(display: Any) -> ColorPalette | ColorScheme:
    scheme = _color_scheme_enum(display)
    return get_palette_for_display(display.panel_ic_type, scheme)


def _palette_color_map(palette: ColorPalette | ColorScheme) -> dict[str, str]:
    colors = (
        palette.colors
        if isinstance(palette, ColorPalette)
        else palette.palette.colors
    )
    out: dict[str, str] = {}
    for name, rgb in colors.items():
        if not isinstance(rgb, (tuple, list)) or len(rgb) < 3:
            continue
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        out[str(name)] = f"#{r:02x}{g:02x}{b:02x}"
    return out


def _render_dimensions(
    pixel_width: int,
    pixel_height: int,
    base_rotation_deg: int,
    user_rotate_deg: int = 0,
) -> tuple[int, int]:
    """Match drawcustom canvas sizing for base + user rotation."""
    effective = (base_rotation_deg + user_rotate_deg) % 360
    if effective in (90, 270):
        return pixel_height, pixel_width
    return pixel_width, pixel_height


def resolve_device_id_for_entry(
    hass: HomeAssistant, entry: OpenDisplayConfigEntry
) -> str | None:
    """Resolve HA device registry id for a config entry."""
    device_registry = dr.async_get(hass)
    by_config_entry = getattr(
        device_registry.devices, "get_devices_for_config_entry_id", None
    )
    if callable(by_config_entry):
        devices = by_config_entry(entry.entry_id)
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
        device = device_registry.async_get_device(
            identifiers={(CONNECTION_BLUETOOTH, variant)}
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
    """Serialize display capabilities for the designer frontend."""
    display = entry.runtime_data.device_config.displays[0]
    scheme_enum = _color_scheme_enum(display)
    palette = _palette_for_display(display)
    color_map = _palette_color_map(palette)
    available_colors = list(color_map.keys())
    base_rotation = _rotation_degrees(display.rotation_enum)
    render_w, render_h = _render_dimensions(
        display.pixel_width,
        display.pixel_height,
        base_rotation,
        user_rotate_deg,
    )

    measured = isinstance(palette, ColorPalette)
    accent = (
        palette.accent
        if isinstance(palette, ColorPalette)
        else scheme_enum.accent_color
    )

    diagonal = display.screen_diagonal_inches
    return {
        "device_id": device_id,
        "pixel_width": int(display.pixel_width),
        "pixel_height": int(display.pixel_height),
        "screen_diagonal_inches": float(diagonal) if diagonal is not None else None,
        "rotation_degrees": int(base_rotation),
        "render_width": int(render_w),
        "render_height": int(render_h),
        # HostCapabilities.color_scheme expects Basic Standard int (0x00–0x04).
        "color_scheme": int(scheme_enum.value),
        "color_scheme_name": str(scheme_enum.name),
        "color_mode": str(scheme_enum.name),
        "panel_ic_type": str(display.panel_ic_type),
        "accent_color": str(accent),
        "available_colors": available_colors,
        "color_map": color_map,
        "palette_measured": bool(measured),
    }
