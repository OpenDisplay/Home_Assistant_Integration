"""Test the OpenDisplay binary sensor platform."""

from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock

from homeassistant.const import STATE_OFF, STATE_ON, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opendisplay.const import DEFAULT_LAN_PORT

from . import DEVICE_CONFIG, make_wifi_device_config

pytestmark = pytest.mark.usefixtures("entity_registry_enabled_by_default")

UPDATE_PENDING = "binary_sensor.opendisplay_1234_update_pending"
WIFI = "binary_sensor.opendisplay_1234_wifi"


@pytest.fixture
def platforms() -> list[Platform]:
    """Only set up the binary sensor platform."""
    return [Platform.BINARY_SENSOR]


async def test_update_pending_created_and_off_when_nothing_queued(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """The update_pending sensor exists and is off with an empty queue."""
    await setup_entry()

    state = hass.states.get(UPDATE_PENDING)
    assert state is not None
    assert state.state == STATE_OFF
    assert state.attributes["queued_at"] is None
    assert state.attributes["expires_at"] is None
    assert state.attributes["attempts"] == 0
    assert state.attributes["last_error"] is None
    assert state.attributes["auth_paused"] is False


async def test_update_pending_turns_on_when_content_is_queued(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Queuing an image drives the sensor on and fills in the timing attributes."""
    await setup_entry()

    manager = mock_config_entry.runtime_data.delivery
    manager.submit_upload(
        prepared=(b"frame", None, MagicMock()),
        refresh_mode=MagicMock(),
        partial_state=MagicMock(),
        use_measured_palettes=False,
        preview_jpeg=b"jpeg",
        device_id=None,
        fingerprint=MagicMock(),
    )
    await hass.async_block_till_done()

    state = hass.states.get(UPDATE_PENDING)
    assert state.state == STATE_ON
    # Both timestamps are rendered as ISO strings, not raw epochs.
    assert state.attributes["queued_at"].startswith("20")
    assert state.attributes["expires_at"].startswith("20")


async def test_update_pending_survives_a_dark_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """The sensor is backed by the manager, so it never goes unavailable.

    This is the point of the entity: it stays readable while the tag is asleep
    and no advertisement has arrived.
    """
    await setup_entry()

    mock_config_entry.runtime_data.delivery.submit_upload(
        prepared=(b"frame", None, MagicMock()),
        refresh_mode=MagicMock(),
        partial_state=MagicMock(),
        use_measured_palettes=False,
        preview_jpeg=b"jpeg",
        device_id=None,
        fingerprint=MagicMock(),
    )
    await hass.async_block_till_done()

    # No advertisement has ever been injected in this test.
    assert hass.states.get(UPDATE_PENDING).state == STATE_ON


async def test_no_wifi_sensor_for_pure_ble_device(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """A tag with no wifi_config and no stored host gets no wifi entity.

    Otherwise every BLE-only tag would carry a permanently-off sensor.
    """
    assert DEVICE_CONFIG.wifi_config is None
    await setup_entry()

    assert entity_registry.async_get(WIFI) is None
    assert entity_registry.async_get(UPDATE_PENDING) is not None


@pytest.mark.parametrize("device_config", [make_wifi_device_config()])
async def test_wifi_sensor_created_for_wifi_capable_device(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """A device advertising a wifi_config packet gets the sensor, off until a host is known."""
    await setup_entry()

    state = hass.states.get(WIFI)
    assert state is not None
    assert state.state == STATE_OFF
    # No endpoint learned yet, so no endpoint attributes.
    assert "ip_address" not in state.attributes


@pytest.mark.parametrize(
    "config_entry_data", [{"host": "192.168.1.50", "port": 1234, "tls": True}]
)
async def test_wifi_sensor_reports_the_lan_endpoint(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """A stored LAN host turns the sensor on and is surfaced as attributes."""
    await setup_entry()

    state = hass.states.get(WIFI)
    assert state.state == STATE_ON
    assert state.attributes["ip_address"] == "192.168.1.50"
    assert state.attributes["port"] == 1234
    assert state.attributes["tls"] is True


@pytest.mark.parametrize("config_entry_data", [{"host": "192.168.1.50"}])
async def test_wifi_sensor_port_defaults_when_unset(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """A host without a port falls back to the default LAN port."""
    await setup_entry()

    state = hass.states.get(WIFI)
    assert state.attributes["port"] == DEFAULT_LAN_PORT
    assert state.attributes["tls"] is False
