"""Repair flows for OpenDisplay."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import CONF_ENCRYPTION_KEY, DOMAIN


def needs_migration_issue_id(entry: ConfigEntry) -> str:
    """Return the repair issue id for a display that has no drive data for its panel."""
    return f"needs_migration_{entry.entry_id}"


@callback
def async_update_needs_migration_issue(
    hass: HomeAssistant, entry: ConfigEntry, needs_migration: bool
) -> None:
    """Raise the issue while the display cannot draw its panel, and remove it after."""
    issue_id = needs_migration_issue_id(entry)
    if not needs_migration:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="needs_migration",
        translation_placeholders={"name": entry.title},
    )


@callback
def async_display_needs_migration(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return whether the last setup found the display without drive data."""
    issue_id = needs_migration_issue_id(entry)
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


class KeyRotatedRepairFlow(RepairsFlow):
    """Show the display's new key, for the other apps that used the old one."""

    def __init__(self, entry_id: str) -> None:
        """Remember which entry's key to show."""
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Start at the confirmation step."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Show the key until the user confirms they have it."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if user_input is not None or entry is None:
            return self.async_create_entry(data={})
        # Read from the entry when the flow opens, so the issue never stores the key.
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": entry.title,
                "key": entry.data.get(CONF_ENCRYPTION_KEY, ""),
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the fix flow for an OpenDisplay issue."""
    assert data is not None
    return KeyRotatedRepairFlow(str(data["entry_id"]))
