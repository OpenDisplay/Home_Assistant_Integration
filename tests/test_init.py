"""Test the OpenDisplay integration setup and unload."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from opendisplay import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    OpenDisplayError,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opendisplay import BASE_PLATFORMS, FLEX_PLATFORMS, _get_platforms
from custom_components.opendisplay.const import (
    CONF_CACHED_STATE,
    CONF_ENCRYPTION_KEY,
    DOMAIN,
)

from . import (
    DEVICE_CONFIG,
    ENCRYPTION_KEY,
    LANDING_URL,
    make_cached_state,
    make_sleepy_device_config,
    make_touch_device_config,
)


async def test_setup_and_unload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test setting up and unloading a config entry."""
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_encrypted_device(
    hass: HomeAssistant,
    mock_encrypted_config_entry: MockConfigEntry,
    mock_opendisplay_device: MagicMock,
    mock_opendisplay_device_class: MagicMock,
) -> None:
    """Test setup passes the encryption key to OpenDisplayDevice."""
    mock_opendisplay_device.is_flex = False
    mock_encrypted_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_encrypted_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_encrypted_config_entry.state is ConfigEntryState.LOADED
    assert mock_opendisplay_device_class.call_args.kwargs[
        "encryption_key"
    ] == bytes.fromhex(ENCRYPTION_KEY)


async def test_setup_device_not_found(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test setup retries when device is not visible."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.opendisplay.async_ble_device_from_address",
        return_value=None,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize(
    "exception",
    [
        BLEConnectionError("connection failed"),
        BLETimeoutError("timeout"),
        OpenDisplayError("device error"),
    ],
)
async def test_setup_connection_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    exception: Exception,
) -> None:
    """Test setup retries on BLE connection errors."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.opendisplay.OpenDisplayDevice",
        return_value=AsyncMock(__aenter__=AsyncMock(side_effect=exception)),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_config_entry.error_reason_translation_key == "setup_connection_error"
    assert mock_config_entry.error_reason_translation_placeholders == {
        "error": str(exception)
    }


async def test_setup_device_registered(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Test that a device is registered in the device registry after setup."""
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    devices = dr.async_entries_for_config_entry(
        device_registry, mock_config_entry.entry_id
    )
    assert len(devices) == 1


@pytest.mark.parametrize(
    ("is_flex", "expect_hw_version"),
    [
        (True, True),
        (False, False),
    ],
)
async def test_setup_device_registry_fields(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_opendisplay_device: MagicMock,
    device_registry: dr.DeviceRegistry,
    is_flex: bool,
    expect_hw_version: bool,
) -> None:
    """Test that hw_version is Flex-only but configuration_url is always set.

    configuration_url is a per-device deep link from ``landing_url()``, which is
    just as useful on a non-Flex tag, so unlike hw_version it is not gated.
    """
    mock_opendisplay_device.is_flex = is_flex
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    devices = dr.async_entries_for_config_entry(
        device_registry, mock_config_entry.entry_id
    )
    assert len(devices) == 1
    device = devices[0]
    assert device.sw_version == "1.2.3"
    assert (device.hw_version is not None) == expect_hw_version
    assert device.configuration_url == LANDING_URL


async def test_unload_cancels_active_upload_task(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test that unloading the entry cancels an in-progress upload task."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    task = hass.async_create_task(asyncio.sleep(3600))
    mock_config_entry.runtime_data.upload_task = task

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert task.cancelled()


@pytest.mark.parametrize(
    "exception",
    [
        AuthenticationFailedError("wrong key"),
        AuthenticationRequiredError("auth required"),
    ],
)
async def test_setup_authentication_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    exception: Exception,
) -> None:
    """Test that auth errors result in SETUP_ERROR and trigger reauth."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.opendisplay.OpenDisplayDevice",
        return_value=AsyncMock(__aenter__=AsyncMock(side_effect=exception)),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert mock_config_entry.error_reason_translation_key == "authentication_error"


async def test_setup_invalid_encryption_key_format(
    hass: HomeAssistant,
) -> None:
    """Test that a malformed stored encryption key triggers reauth."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        title="OpenDisplay 1234",
        data={CONF_ENCRYPTION_KEY: "not-valid-hex!"},
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "authentication_error"


# --- deep-sleep setup paths ------------------------------------------------


@pytest.mark.parametrize(
    "config_entry_data",
    [{CONF_CACHED_STATE: make_cached_state(make_sleepy_device_config())}],
)
@pytest.mark.parametrize("device_config", [make_sleepy_device_config()])
async def test_setup_from_cache_when_a_sleepy_device_is_dark(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_opendisplay_device_class: MagicMock,
) -> None:
    """A sleeping tag sets up from cached state instead of failing.

    Without this a deep-sleeping device would be unavailable after every
    Home Assistant restart until it happened to wake.
    """
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.opendisplay.async_ble_device_from_address",
        return_value=None,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    # Set up without ever opening a connection.
    mock_opendisplay_device_class.assert_not_called()
    # And schedules a re-interrogation for the next time the tag is awake.
    assert mock_config_entry.runtime_data.config_resync_pending is True


@pytest.mark.parametrize("device_config", [make_sleepy_device_config()])
async def test_no_cache_still_fails_when_the_device_is_dark(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A sleepy device with nothing cached has nothing to set up from."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.opendisplay.async_ble_device_from_address",
        return_value=None,
    ):
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize(
    "config_entry_data", [{CONF_CACHED_STATE: make_cached_state()}]
)
async def test_cache_is_not_used_for_a_non_sleepy_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A USB tag that is unreachable is genuinely broken, so do not paper over it.

    The cache exists for devices that are expected to be dark, not as a general
    fallback for a connect failure.
    """
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.opendisplay.async_ble_device_from_address",
        return_value=None,
    ):
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_deadline_is_treated_as_a_connect_failure(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_opendisplay_device: MagicMock,
) -> None:
    """A wedged BLE link must not stall setup forever."""
    mock_opendisplay_device.read_firmware_version.side_effect = TimeoutError
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


# --- platform selection ----------------------------------------------------


@pytest.mark.parametrize(
    ("is_flex", "device_config", "expected"),
    [
        (True, DEVICE_CONFIG, set(FLEX_PLATFORMS)),
        (True, make_touch_device_config(), set(FLEX_PLATFORMS)),
        (False, DEVICE_CONFIG, set(BASE_PLATFORMS)),
        (False, make_touch_device_config(), set(BASE_PLATFORMS)),
    ],
    ids=["flex", "flex-with-touch", "base", "base-with-touch"],
)
async def test_platform_selection(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    expected: set[Platform],
) -> None:
    """The platform set depends only on the device class, not on touch hardware.

    Touch events are a Flex feature: EVENT comes from FLEX_PLATFORMS, so a base
    model does not get it even when the device config reports a touch
    controller.
    """
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert set(_get_platforms(mock_config_entry.runtime_data)) == expected
