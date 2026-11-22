"""Repair flows for OpenDisplay."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from .const import CONF_ENCRYPTION_KEY


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
