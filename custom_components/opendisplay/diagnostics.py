"""Diagnostics support for OpenDisplay."""

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import OpenDisplayConfigEntry
from .const import CONF_HOST, CONF_PORT, CONF_TLS

TO_REDACT = {
    "ssid",
    "password",
    "server_url",
    "serial_number",
    "friendly_name",
    "device_location",
    "device_id",
    "custom_string_1",
    "custom_string_2",
    "custom_string_3",
    "encryption_key",
    # Raw bytes of config packets the library does not recognise: unknown
    # content, so never expose it.
    "unparsed_tail",
}


def _asdict(obj: Any) -> Any:
    """Recursively convert a dataclass to a dict, encoding bytes as hex strings."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _asdict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, list):
        return [_asdict(item) for item in obj]
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OpenDisplayConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    fw = runtime.firmware
    profile = runtime.sleep_profile

    sleep_diag: dict[str, Any] = {
        "is_sleepy": profile.is_sleepy,
        "deep_sleep_enabled": profile.deep_sleep_enabled,
        "deep_sleep_time_seconds": profile.deep_sleep_time_seconds,
        "sleep_timeout_ms": profile.sleep_timeout_ms,
        "missed_cycles": profile.missed_cycles,
        "queue_timeout_hours": profile.queue_timeout_hours,
        "availability_interval_s": profile.availability_interval,
        "config_resync_pending": runtime.config_resync_pending,
    }

    # Delivery slot state only: timestamps/attempts, never queued image bytes.
    if runtime.delivery is not None:
        state = runtime.delivery.state
        sleep_diag["delivery"] = {
            "pending": state.pending,
            "queued_at": state.queued_at,
            "expires_at": state.expires_at,
            "attempts": state.attempts,
            "last_error": state.last_error,
            "auth_paused": state.auth_paused,
        }

    # Transport diagnostics: WiFi/LAN endpoint (host/port/tls are not secret; the
    # redaction set already covers ssid/password/server_url), the last transport a
    # delivery used, and how recently the device was seen via mDNS. host is None
    # for a pure-BLE entry.
    system = getattr(runtime.device_config, "system", None)
    transport_diag: dict[str, Any] = {
        "host": entry.data.get(CONF_HOST),
        "port": entry.data.get(CONF_PORT),
        "tls": entry.data.get(CONF_TLS),
        "last_transport": getattr(runtime, "last_transport", None),
        "mdns_last_seen": getattr(runtime, "mdns_last_seen", None),
        "communication_modes": getattr(system, "communication_modes", None),
    }

    return {
        "firmware": {
            "major": fw["major"],
            "minor": fw["minor"],
            "patch": fw.get("patch"),
            "sha": fw["sha"],
        },
        "is_flex": runtime.is_flex,
        "sleep": sleep_diag,
        "transport": transport_diag,
        "device_config": async_redact_data(_asdict(runtime.device_config), TO_REDACT),
    }
