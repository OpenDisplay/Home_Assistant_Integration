"""Test the OpenDisplay diagnostics."""

from collections.abc import Awaitable, Callable
import dataclasses
import struct
from unittest.mock import MagicMock, patch

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from opendisplay import LengthPrefixedPacket
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from . import DEVICE_CONFIG, make_wifi_device_config

# A FindMy packet (key material), one drive mode the library decodes, and one
# length-prefixed packet it does not know.
CONFIG_WITH_PACKETS = dataclasses.replace(
    DEVICE_CONFIG,
    findmy_config_raw=b"\x00\x2d" + b"\x11" * 128,
    length_prefixed_packets=[
        LengthPrefixedPacket(
            0,
            0xAE,
            struct.pack(
                "<BB12sBBbbB8s8sH",
                0,
                0,
                b"full",
                1,
                1,
                0,
                50,
                0,
                b"\x00" + b"\xff" * 7,
                bytes(8),
                3,
            )
            + b"\x01\x12\x00",
        ),
        LengthPrefixedPacket(1, 0xF0, b"\x22" * 8),
    ],
)


@pytest.mark.parametrize("device_config", [CONFIG_WITH_PACKETS], ids=["drive_packets"])
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


async def test_diagnostics_of_an_entry_that_is_not_loaded(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
) -> None:
    """While the entry is not loaded (setup retry, a reload) there is no runtime data."""
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.opendisplay.async_ble_device_from_address",
        return_value=None,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    result = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )

    assert result["state"] == "setup_retry"
    assert result["transport"]["host"] is None
