"""Test the OpenDisplay diagnostics."""

from collections.abc import Awaitable, Callable
import dataclasses
from unittest.mock import MagicMock

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from . import DEVICE_CONFIG, make_wifi_device_config


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    mock_opendisplay_device: MagicMock,
    snapshot: SnapshotAssertion,
) -> None:
    """Test diagnostics output matches snapshot."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )
    assert result == snapshot


@pytest.mark.parametrize("device_config", [make_wifi_device_config()])
async def test_diagnostics_redacts_wifi_credentials(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """WiFi credentials never reach a diagnostics download.

    The default device config carries no wifi_config, so these keys are only
    reachable with a WiFi-capable device.
    """
    await setup_entry()

    result = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )

    wifi = result["device_config"]["wifi_config"]
    assert wifi["ssid"] == REDACTED
    assert wifi["password"] == REDACTED
    assert wifi["server_url"] == REDACTED


@pytest.mark.parametrize(
    "device_config",
    [dataclasses.replace(DEVICE_CONFIG, unparsed_tail=b"\xde\xad\xbe\xef")],
)
async def test_diagnostics_redacts_unparsed_config_bytes(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Raw bytes of config packets the library does not recognise never leak.

    Their content is unknown, so they could hold anything; diagnostics would
    otherwise render them as hex.
    """
    await setup_entry()

    result = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )

    assert result["device_config"]["unparsed_tail"] == REDACTED
