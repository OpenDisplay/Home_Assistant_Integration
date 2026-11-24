"""Test the OpenDisplay repair flows."""

import dataclasses
from pathlib import Path
from typing import Any
from unittest.mock import patch

from homeassistant.components.repairs import DOMAIN as REPAIRS_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from opendisplay.panels import MigrationPlan, TargetClass
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.repairs import (
    process_repair_fix_flow,
    start_repair_fix_flow,
)
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.opendisplay.const import CONF_ENCRYPTION_KEY, DOMAIN
from custom_components.opendisplay.keystore import EntryKeyStore, key_rotated_issue_id
from custom_components.opendisplay.repairs import (
    MigrationFailedError,
    MigrationUnavailableError,
    async_update_needs_migration_issue,
    needs_migration_issue_id,
)

_REPAIRS = "custom_components.opendisplay.repairs"

OLD = bytes.fromhex("11" * 16)
NEW = bytes.fromhex("22" * 16)


@pytest.fixture
def config_entry_data() -> dict[str, Any]:
    """Return entry data holding a migrated 2.x key."""
    return {CONF_ENCRYPTION_KEY: OLD.hex()}


async def test_the_rotated_key_issue_shows_the_new_key_once(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The fix flow shows the key the display holds now, and resolves the issue."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert await async_setup_component(hass, REPAIRS_DOMAIN, {})

    store = EntryKeyStore(hass, mock_config_entry)
    store.stage("OD4064C9", NEW, "rotation")
    store.promote("OD4064C9", NEW)
    issue_id = key_rotated_issue_id(mock_config_entry)

    client = await hass_client()
    flow = await start_repair_fix_flow(client, DOMAIN, issue_id)
    assert flow["step_id"] == "confirm"
    assert flow["description_placeholders"]["key"] == NEW.hex()

    flow = await process_repair_fix_flow(client, flow["flow_id"])
    assert flow["type"] == "create_entry"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


def _plan(**changes: Any) -> MigrationPlan:
    plan = MigrationPlan(
        needed=True,
        blocked=False,
        reason="adds drive data",
        panel_ic_type=0x0023,
        target_class=TargetClass.DEFAULT,
    )
    return dataclasses.replace(plan, **changes)


@pytest.fixture
async def migration_issue(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> str:
    """Set the entry up with a display that needs migration; return the issue id."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert await async_setup_component(hass, REPAIRS_DOMAIN, {})
    async_update_needs_migration_issue(hass, mock_config_entry, True)
    return needs_migration_issue_id(mock_config_entry)


async def test_the_migration_fix_writes_the_drive_data_and_reloads(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    migration_issue: str,
) -> None:
    """The user sees what is written, confirms, and the display is reloaded."""
    with (
        patch(f"{_REPAIRS}.async_plan_migration", return_value=_plan()),
        patch(f"{_REPAIRS}.async_migrate", return_value=Path("b.json")) as migrate,
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        client = await hass_client()
        flow = await start_repair_fix_flow(client, DOMAIN, migration_issue)
        assert flow["step_id"] == "confirm"
        assert flow["description_placeholders"]["panel"] == "0x0023"

        flow = await process_repair_fix_flow(client, flow["flow_id"])

    assert flow["type"] == "create_entry"
    migrate.assert_awaited_once()
    reload.assert_called_once_with(mock_config_entry.entry_id)
    assert issue_registry.async_get_issue(DOMAIN, migration_issue) is None


async def test_drive_data_nobody_watched_on_glass_gets_its_own_warning(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    migration_issue: str,
) -> None:
    """Drive data that only replays what 2.x sent is offered with a warning."""
    with patch(
        f"{_REPAIRS}.async_plan_migration",
        return_value=_plan(needs_confirmation=True),
    ):
        client = await hass_client()
        flow = await start_repair_fix_flow(client, DOMAIN, migration_issue)

    assert flow["step_id"] == "confirm_untested"


async def test_a_blocked_migration_says_why(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    migration_issue: str,
) -> None:
    """Nothing is written when the library cannot migrate the config."""
    with (
        patch(
            f"{_REPAIRS}.async_plan_migration",
            return_value=_plan(blocked=True, reason="no display configured"),
        ),
        patch(f"{_REPAIRS}.async_migrate") as migrate,
    ):
        client = await hass_client()
        flow = await start_repair_fix_flow(client, DOMAIN, migration_issue)

    assert flow["type"] == "abort"
    assert flow["reason"] == "migration_blocked"
    assert flow["description_placeholders"] == {"reason": "no display configured"}
    migrate.assert_not_called()


async def test_an_unreachable_display_is_asked_to_wake_first(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    migration_issue: str,
) -> None:
    """A sleeping display cannot be planned for; the user tries again once it is awake."""
    with patch(
        f"{_REPAIRS}.async_plan_migration",
        side_effect=MigrationUnavailableError,
    ):
        client = await hass_client()
        flow = await start_repair_fix_flow(client, DOMAIN, migration_issue)

    assert flow["type"] == "abort"
    assert flow["reason"] == "cannot_connect"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MigrationUnavailableError, "cannot_connect"),
        (MigrationFailedError, "write_failed"),
    ],
)
async def test_a_failed_write_keeps_the_issue_and_can_be_retried(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    issue_registry: ir.IssueRegistry,
    migration_issue: str,
    error: type[Exception],
    expected: str,
) -> None:
    """The confirm step shows the error and stays open for another try."""
    with (
        patch(f"{_REPAIRS}.async_plan_migration", return_value=_plan()),
        patch(f"{_REPAIRS}.async_migrate", side_effect=error),
    ):
        client = await hass_client()
        flow = await start_repair_fix_flow(client, DOMAIN, migration_issue)
        flow = await process_repair_fix_flow(client, flow["flow_id"])

    assert flow["type"] == "form"
    assert flow["errors"] == {"base": expected}
    assert issue_registry.async_get_issue(DOMAIN, migration_issue) is not None


async def test_a_display_already_migrated_resolves_the_issue(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    migration_issue: str,
) -> None:
    """Migrated elsewhere since setup: nothing to write, the entry is reloaded."""
    with (
        patch(
            f"{_REPAIRS}.async_plan_migration",
            return_value=_plan(needed=False, reason="already has drive data"),
        ),
        patch(f"{_REPAIRS}.async_migrate") as migrate,
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        client = await hass_client()
        flow = await start_repair_fix_flow(client, DOMAIN, migration_issue)

    assert flow["type"] == "create_entry"
    migrate.assert_not_called()
    reload.assert_called_once_with(mock_config_entry.entry_id)
    assert issue_registry.async_get_issue(DOMAIN, migration_issue) is None
