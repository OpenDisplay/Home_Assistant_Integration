"""Test the OpenDisplay repair flows."""

from typing import Any

from homeassistant.components.repairs import DOMAIN as REPAIRS_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.repairs import (
    process_repair_fix_flow,
    start_repair_fix_flow,
)
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.opendisplay.const import CONF_ENCRYPTION_KEY, DOMAIN
from custom_components.opendisplay.keystore import EntryKeyStore, key_rotated_issue_id

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
