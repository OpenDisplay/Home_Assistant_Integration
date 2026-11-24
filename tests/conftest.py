"""OpenDisplay test fixtures."""

from collections.abc import Awaitable, Callable, Generator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from opendisplay import GlobalConfig
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.bluetooth import generate_ble_device
from pytest_homeassistant.typing import RecorderInstanceContextManager

from custom_components.opendisplay import BASE_PLATFORMS, FLEX_PLATFORMS
from custom_components.opendisplay.const import CONF_ENCRYPTION_KEY, DOMAIN

from . import (
    BUTTON_DEVICE_CONFIG,
    DEVICE_CONFIG,
    ENCRYPTION_KEY,
    FIRMWARE_VERSION,
    LANDING_URL,
    TEST_ADDRESS,
    TEST_TITLE,
    make_binary_inputs,
    make_button_device_config,
)

# Every module that imports these by name needs its own patch, since patching
# the source module would not reach the already-bound references.
_DEVICE_NAMESPACES = (
    "custom_components.opendisplay.update.OpenDisplayDevice",
    "custom_components.opendisplay.services.OpenDisplayDevice",
    "custom_components.opendisplay.transport.OpenDisplayDevice",
    "custom_components.opendisplay.delivery.OpenDisplayDevice",
)
# Modules that connect with opendisplay.connect(). Its device's ``legacy`` is the
# OpenDisplayDevice mock below, so tests keep asserting on that one mock.
_CONNECT_NAMESPACES = (
    "custom_components.opendisplay.connect",
    "custom_components.opendisplay.config_flow.connect",
    "custom_components.opendisplay.transport.connect",
    "custom_components.opendisplay.repairs.connect",
)
_BLE_DEVICE_NAMESPACES = (
    "custom_components.opendisplay.async_ble_device_from_address",
    "custom_components.opendisplay.config_flow.async_ble_device_from_address",
    "custom_components.opendisplay.transport.async_ble_device_from_address",
    "custom_components.opendisplay.update.async_ble_device_from_address",
    "custom_components.opendisplay.repairs.async_ble_device_from_address",
)


@pytest.fixture(autouse=True)
def mock_bluetooth(enable_bluetooth: None) -> None:
    """Auto mock bluetooth."""


# manifest.json depends on recorder (services.py reads history for plot
# elements), so every entry setup pulls it in. This has to override
# `mock_recorder_before_hass` rather than be a plain autouse fixture, because
# recorder_db_url asserts it runs before the hass fixture.
@pytest.fixture
async def mock_recorder_before_hass(
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """Set up the recorder before hass, as its db fixture requires."""


@pytest.fixture(autouse=True)
def mock_recorder(recorder_mock: object) -> None:
    """Give every test an in-memory recorder instance."""


@contextmanager
def _patch_each(targets: tuple[str, ...], **kwargs: Any) -> Generator[None]:
    """Patch several dotted targets with the same replacement."""
    with ExitStack() as stack:
        for target in targets:
            stack.enter_context(patch(target, **kwargs))
        yield


@pytest.fixture(autouse=True)
def mock_ble_device() -> Generator[None]:
    """Mock the BLE device being visible."""
    ble_device = generate_ble_device(TEST_ADDRESS, TEST_TITLE)
    with _patch_each(_BLE_DEVICE_NAMESPACES, return_value=ble_device):
        yield


@pytest.fixture
def device_config() -> GlobalConfig:
    """Return the GlobalConfig the mocked device reports; override to vary hardware."""
    return DEVICE_CONFIG


@pytest.fixture
def is_flex() -> bool:
    """Whether the mocked device is a Flex. Override for base-model tests."""
    return True


@pytest.fixture
def mock_opendisplay_device_class(
    device_config: GlobalConfig, is_flex: bool
) -> Generator[MagicMock]:
    """Yield the OpenDisplayDevice class mock (for asserting constructor args)."""
    with (
        patch(_DEVICE_NAMESPACES[0], autospec=True) as mock_class,
        _patch_each(_DEVICE_NAMESPACES[1:], new=mock_class),
    ):
        mock_device = mock_class.return_value
        mock_device.__aenter__.return_value = mock_device
        mock_device.read_firmware_version.return_value = FIRMWARE_VERSION
        mock_device.config = device_config
        mock_device.is_flex = is_flex
        # Becomes the device registry's configuration_url, which rejects a
        # MagicMock.
        mock_device.landing_url.return_value = LANDING_URL
        yield mock_class


@pytest.fixture
def mock_opendisplay_connect(
    mock_opendisplay_device_class: MagicMock, device_config: GlobalConfig
) -> Generator[MagicMock]:
    """Yield the opendisplay.connect mock (for asserting its arguments)."""
    with (
        patch(_CONNECT_NAMESPACES[0]) as mock_fn,
        _patch_each(_CONNECT_NAMESPACES[1:], new=mock_fn),
    ):
        connected = mock_fn.return_value
        connected.legacy = mock_opendisplay_device_class.return_value

        async def _enter(*_args: Any) -> MagicMock:
            # Like Device.__aenter__: the legacy device connects first, so an
            # error a test puts on the legacy mock's __aenter__ surfaces here.
            await connected.legacy.__aenter__()
            # Both hold the config read on entry; tests set it on the legacy mock.
            connected.config.cached = connected.legacy.config
            return connected

        connected.__aenter__.side_effect = _enter
        connected.profile.firmware = FIRMWARE_VERSION
        connected.profile.needs_migration = False
        yield mock_fn


@pytest.fixture(autouse=True)
def mock_opendisplay_device(
    mock_opendisplay_device_class: MagicMock, mock_opendisplay_connect: MagicMock
) -> MagicMock:
    """Mock the device for setup entry; yields the OpenDisplayDevice instance mock."""
    return mock_opendisplay_device_class.return_value


@pytest.fixture
def config_entry_data() -> dict[str, Any]:
    """Return the config entry's data; override to store a LAN endpoint or key."""
    return {}


@pytest.fixture
def config_entry_options() -> dict[str, Any]:
    """Return the config entry's options; override to configure the integration."""
    return {}


@pytest.fixture
def mock_config_entry(
    config_entry_data: dict[str, Any], config_entry_options: dict[str, Any]
) -> MockConfigEntry:
    """Create a mock config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_ADDRESS,
        title=TEST_TITLE,
        data=config_entry_data,
        options=config_entry_options,
    )


@pytest.fixture
def mock_button_config_entry(mock_opendisplay_device: MagicMock) -> MockConfigEntry:
    """Create a mock config entry for a device with one button configured."""
    mock_opendisplay_device.config = BUTTON_DEVICE_CONFIG
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_ADDRESS,
        title=TEST_TITLE,
        data={},
    )


@pytest.fixture
def mock_two_button_config_entry(mock_opendisplay_device: MagicMock) -> MockConfigEntry:
    """Create a mock config entry for a device with two buttons configured."""
    mock_opendisplay_device.config = make_button_device_config(
        [make_binary_inputs(input_flags=0x03)]
    )
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_ADDRESS,
        title=TEST_TITLE,
        data={},
    )


@pytest.fixture
def mock_three_button_config_entry(
    mock_opendisplay_device: MagicMock,
) -> MockConfigEntry:
    """Create a mock config entry for a device with three buttons configured."""
    mock_opendisplay_device.config = make_button_device_config(
        [make_binary_inputs(input_flags=0x07)]
    )
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_ADDRESS,
        title=TEST_TITLE,
        data={},
    )


@pytest.fixture
def mock_multi_instance_config_entry(
    mock_opendisplay_device: MagicMock,
) -> MockConfigEntry:
    """Create a mock config entry with two binary_inputs instances.

    Instance 0: byte_index=0, buttons 0 and 1 active -> Button 1, Button 2
    Instance 1: byte_index=1, button 0 active        -> Button 3
    """
    mock_opendisplay_device.config = make_button_device_config(
        [
            make_binary_inputs(instance_number=0, byte_index=0, input_flags=0x03),
            make_binary_inputs(instance_number=1, byte_index=1, input_flags=0x01),
        ]
    )
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_ADDRESS,
        title=TEST_TITLE,
        data={},
    )


@pytest.fixture
def mock_encrypted_config_entry() -> MockConfigEntry:
    """Create a mock config entry with an encryption key."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_ADDRESS,
        title=TEST_TITLE,
        data={CONF_ENCRYPTION_KEY: ENCRYPTION_KEY},
    )


@pytest.fixture
def platforms() -> list[Platform]:
    """Platforms to set up for the test. Override in test modules to scope setup."""
    return [*FLEX_PLATFORMS]


@pytest.fixture
def setup_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    platforms: list[Platform],
) -> Callable[[], Awaitable[None]]:
    """Return an async callable that sets up the integration with `platforms` only."""

    async def _setup() -> None:
        # intersect the platform lists so a platform that is not defined for the
        # device type is never set up during tests
        flex_platforms = [p for p in FLEX_PLATFORMS if p in platforms]
        base_platforms = [p for p in BASE_PLATFORMS if p in platforms]

        mock_config_entry.add_to_hass(hass)
        with (
            patch("custom_components.opendisplay.BASE_PLATFORMS", base_platforms),
            patch("custom_components.opendisplay.FLEX_PLATFORMS", flex_platforms),
        ):
            assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    return _setup
