"""Test the OpenDisplay passive-BLE coordinator."""

from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant.common import MockConfigEntry

from . import TEST_ADDRESS, make_service_info, make_v1_service_info
from .bluetooth import inject_bluetooth_service_info


@pytest.fixture
def platforms() -> list[Platform]:
    """No platforms; these tests drive the coordinator directly."""
    return []


def _coordinator(entry: MockConfigEntry):
    return entry.runtime_data.coordinator


async def test_advertisement_populates_the_update(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """A parsed advertisement is stored with its address, rssi and timestamp."""
    await setup_entry()

    inject_bluetooth_service_info(hass, make_v1_service_info())
    await hass.async_block_till_done()

    data = _coordinator(mock_config_entry).data
    assert data.address == TEST_ADDRESS
    assert data.rssi == -60
    assert data.last_seen is not None


async def test_advertisement_from_another_manufacturer_is_ignored(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Only OpenDisplay manufacturer data is parsed."""
    await setup_entry()

    inject_bluetooth_service_info(
        hass, make_service_info(manufacturer_data={0x1234: b"\x00" * 14})
    )
    await hass.async_block_till_done()

    assert _coordinator(mock_config_entry).data is None


async def test_unparseable_advertisement_leaves_the_last_update_intact(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """A truncated payload is dropped rather than clobbering good data."""
    await setup_entry()

    inject_bluetooth_service_info(hass, make_v1_service_info())
    await hass.async_block_till_done()
    good = _coordinator(mock_config_entry).data

    inject_bluetooth_service_info(
        hass, make_service_info(manufacturer_data={0x2446: b"\x00"})
    )
    await hass.async_block_till_done()

    assert _coordinator(mock_config_entry).data is good


# --- device seen -----------------------------------------------------------


async def test_device_seen_fires_for_every_parsed_advertisement(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """The wake trigger fires whenever the tag is heard from."""
    await setup_entry()
    seen = MagicMock()
    _coordinator(mock_config_entry).async_subscribe_device_seen(seen)

    inject_bluetooth_service_info(hass, make_v1_service_info(loop_counter=0x11))
    await hass.async_block_till_done()
    inject_bluetooth_service_info(hass, make_v1_service_info(loop_counter=0x21))
    await hass.async_block_till_done()

    assert seen.call_count == 2


async def test_unsubscribing_stops_device_seen_callbacks(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """The returned callback detaches the subscriber."""
    await setup_entry()
    seen = MagicMock()
    unsubscribe = _coordinator(mock_config_entry).async_subscribe_device_seen(seen)
    unsubscribe()

    inject_bluetooth_service_info(hass, make_v1_service_info())
    await hass.async_block_till_done()

    seen.assert_not_called()


# --- reboot detection ------------------------------------------------------


async def test_reboot_fires_on_a_false_to_true_edge(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """The device raises the flag on boot, so a rising edge means it rebooted."""
    await setup_entry()
    rebooted = MagicMock()
    _coordinator(mock_config_entry).async_subscribe_reboot(rebooted)

    inject_bluetooth_service_info(hass, make_v1_service_info(reboot=False))
    await hass.async_block_till_done()
    inject_bluetooth_service_info(
        hass, make_v1_service_info(reboot=True, loop_counter=0x21)
    )
    await hass.async_block_till_done()

    rebooted.assert_called_once()


async def test_the_first_advertisement_never_reports_a_reboot(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Setup has already synced this boot, so an initial raised flag is expected.

    Firing here would make every restart look like a device reboot and trigger
    a pointless reconnect.
    """
    await setup_entry()
    rebooted = MagicMock()
    _coordinator(mock_config_entry).async_subscribe_reboot(rebooted)

    inject_bluetooth_service_info(hass, make_v1_service_info(reboot=True))
    await hass.async_block_till_done()

    rebooted.assert_not_called()


async def test_a_flag_that_stays_raised_only_fires_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """A device that never clears the flag must not fire on every advertisement."""
    await setup_entry()
    rebooted = MagicMock()
    _coordinator(mock_config_entry).async_subscribe_reboot(rebooted)

    inject_bluetooth_service_info(hass, make_v1_service_info(reboot=False))
    await hass.async_block_till_done()
    for loop in (0x21, 0x31, 0x41):
        inject_bluetooth_service_info(
            hass, make_v1_service_info(reboot=True, loop_counter=loop)
        )
        await hass.async_block_till_done()

    rebooted.assert_called_once()
