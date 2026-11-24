"""Repair flows for OpenDisplay.

A firmware 3.0 display updated from 2.x keeps its config, which has no drive data for its panel,
so it draws nothing (READ_IDENT NEEDS_MIGRATION). py-opendisplay plans the change from its
bundled panel database and keeps every other packet; the fix flow saves a backup and writes it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import contextlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import slugify
import homeassistant.util.dt as dt_util

from opendisplay import OpenDisplayError, connect
from opendisplay.models.config import GlobalConfig
from opendisplay.models.config_json import config_to_json
from opendisplay.panels import MigrationPlan, PanelDatabase, PanelEntry, apply_migration
from opendisplay.setup.configure import (
    ConfigMismatchError,
    strip_secrets,
    write_and_verify,
)

from .ble_lock import ble_connection
from .const import CONF_ENCRYPTION_KEY, DOMAIN, SETUP_DEADLINE_S
from .keystore import EntryKeyStore

if TYPE_CHECKING:
    from opendisplay.device.api import Device

    from . import OpenDisplayConfigEntry

BACKUP_DIR = ("opendisplay", "backups")


class MigrationUnavailableError(HomeAssistantError):
    """The display did not answer; it may be asleep."""


class MigrationFailedError(HomeAssistantError):
    """The config could not be migrated; the display keeps what it had."""


@contextlib.asynccontextmanager
async def _async_connected(
    hass: HomeAssistant, entry: OpenDisplayConfigEntry
) -> AsyncIterator[Device]:
    """Connect over BLE; a display that does not answer raises ``MigrationUnavailableError``."""
    # Imported here: __init__ imports this module.
    from . import _get_encryption_key

    address = entry.unique_id
    assert address is not None
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise MigrationUnavailableError
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(ble_connection(address, "config migration"))
        try:
            async with asyncio.timeout(SETUP_DEADLINE_S):
                device = await stack.enter_async_context(
                    connect(
                        address=address,
                        ble_device=ble_device,
                        encryption_key=_get_encryption_key(entry),
                        key_store=EntryKeyStore(hass, entry),
                    )
                )
        except (OpenDisplayError, TimeoutError) as err:
            raise MigrationUnavailableError from err
        yield device


async def _async_plan(hass: HomeAssistant, device: Device) -> MigrationPlan:
    database = await hass.async_add_executor_job(PanelDatabase.load)
    return device.config.plan_migration(database)


async def async_plan_migration(
    hass: HomeAssistant, entry: OpenDisplayConfigEntry
) -> MigrationPlan:
    """Return what the display's config needs, from the config it holds now."""
    async with _async_connected(hass, entry) as device:
        return await _async_plan(hass, device)


def _write_backup(directory: Path, stem: str, config: GlobalConfig) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    document = json.dumps(config_to_json(strip_secrets(config)), indent=2)
    for attempt in range(1, 100):
        path = directory / (
            f"{stem}.json" if attempt == 1 else f"{stem}-{attempt}.json"
        )
        # Owner-readable only and never overwritten, as `opendisplay backup-config` writes it.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(document)
        return path
    raise FileExistsError(directory / f"{stem}.json")


async def async_migrate(hass: HomeAssistant, entry: OpenDisplayConfigEntry) -> Path:
    """Back up the display's config, write it with drive data, and return the backup's path.

    Raises:
        MigrationUnavailableError: The display did not answer.
        MigrationFailedError: The plan is blocked, the write did not read back, or the
            display still cannot draw afterwards.

    """
    async with _async_connected(hass, entry) as device:
        plan = await _async_plan(hass, device)
        config = device.config.cached
        if not plan.needed or plan.blocked or config is None:
            raise MigrationFailedError(plan.reason)
        stem = f"{slugify(entry.title)}-{dt_util.now().strftime('%Y%m%d-%H%M%S')}"
        try:
            backup = await hass.async_add_executor_job(
                _write_backup, Path(hass.config.path(*BACKUP_DIR)), stem, config
            )
        except OSError as err:
            raise MigrationFailedError(f"the backup could not be saved: {err}") from err
        try:
            await write_and_verify(device, apply_migration(config, plan))
        except (ConfigMismatchError, OpenDisplayError, TimeoutError) as err:
            raise MigrationFailedError(str(err)) from err
        if device.profile.needs_migration:
            raise MigrationFailedError(
                "the display still reports that it needs migration"
            )
    return backup


_NEEDS_MIGRATION_PREFIX = "needs_migration_"


def needs_migration_issue_id(entry: ConfigEntry) -> str:
    """Return the repair issue id for a display that has no drive data for its panel."""
    return f"{_NEEDS_MIGRATION_PREFIX}{entry.entry_id}"


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
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="needs_migration",
        translation_placeholders={"name": entry.title},
        data={"entry_id": entry.entry_id},
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


def _panel_name(plan: MigrationPlan) -> str:
    if isinstance(plan.entry, PanelEntry):
        return f"{plan.entry.name} (0x{plan.entry.panel_ic_type:04X})"
    return f"0x{plan.panel_ic_type:04X}" if plan.panel_ic_type is not None else "?"


class NeedsMigrationRepairFlow(RepairsFlow):
    """Add the panel's drive data to the display's config, after a backup."""

    def __init__(self, entry_id: str) -> None:
        """Remember which entry's display to migrate."""
        self._entry_id = entry_id
        self._placeholders: dict[str, str] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Read the display's config and show what would be written."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_create_entry(data={})
        try:
            plan = await async_plan_migration(self.hass, entry)
        except MigrationUnavailableError:
            return self.async_abort(
                reason="cannot_connect", description_placeholders={"name": entry.title}
            )
        if not plan.needed:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_create_entry(data={})
        if plan.blocked:
            return self.async_abort(
                reason="migration_blocked",
                description_placeholders={"reason": plan.reason},
            )
        self._placeholders = {"name": entry.title, "panel": _panel_name(plan)}
        if plan.needs_confirmation:
            return await self.async_step_confirm_untested()
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Write the drive data once the user confirms."""
        return await self._async_write("confirm", user_input)

    async def async_step_confirm_untested(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Write drive data that nobody has watched draw on this panel yet."""
        return await self._async_write("confirm_untested", user_input)

    async def _async_write(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> RepairsFlowResult:
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if user_input is not None and entry is not None:
            try:
                await async_migrate(self.hass, entry)
            except MigrationUnavailableError:
                errors["base"] = "cannot_connect"
            except MigrationFailedError:
                errors["base"] = "write_failed"
            else:
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id=step_id, description_placeholders=self._placeholders, errors=errors
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the fix flow for an OpenDisplay issue."""
    assert data is not None
    entry_id = str(data["entry_id"])
    if issue_id.startswith(_NEEDS_MIGRATION_PREFIX):
        return NeedsMigrationRepairFlow(entry_id)
    return KeyRotatedRepairFlow(entry_id)
