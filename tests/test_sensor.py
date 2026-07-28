"""Test the OpenDisplay sensor platform."""

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from habluetooth import CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS
from homeassistant.components.bluetooth.const import UNAVAILABLE_TRACK_SECONDS
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from opendisplay import voltage_to_percent
from opendisplay.models.config import DisplayConfig
from opendisplay.models.enums import CapacityEstimator, PowerMode
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.opendisplay.sensor import (
    _RESOLUTION_DESCRIPTION,
    _TEMPERATURE_DESCRIPTION,
    OpenDisplayResolutionSensor,
    _sht40_descriptions,
)
from tests.bluetooth import (
    inject_bluetooth_service_info,
    patch_all_discovered_devices,
    patch_bluetooth_time,
)

from . import (
    DEVICE_CONFIG,
    TEST_ADDRESS,
    VALID_SERVICE_INFO,
    make_service_info,
    make_sht40_device_config,
    make_sht40_service_info,
)

pytestmark = pytest.mark.usefixtures("entity_registry_enabled_by_default")


@pytest.fixture
def platforms() -> list[Platform]:
    """Only set up the sensor platform."""
    return [Platform.SENSOR]


async def test_sensors_before_data(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Test that sensors are created but unavailable before data arrives."""
    await setup_entry()

    # All sensors exist but coordinator has no data yet
    assert hass.states.get("sensor.opendisplay_1234_chip_temperature") is not None
    assert (
        hass.states.get("sensor.opendisplay_1234_chip_temperature").state
        == STATE_UNAVAILABLE
    )


# last_seen renders wall-clock time, so the snapshot needs a fixed clock.
@pytest.mark.freeze_time("2026-01-01 00:00:00+00:00")
async def test_sensor_entities_usb_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Test sensor entities for a USB-powered Flex device."""
    await setup_entry()

    inject_bluetooth_service_info(hass, VALID_SERVICE_INFO)
    await hass.async_block_till_done()

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


# last_seen renders wall-clock time, so the snapshot needs a fixed clock.
@pytest.mark.freeze_time("2026-01-01 00:00:00+00:00")
async def test_sensor_entities_battery_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_opendisplay_device: MagicMock,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Test sensor entities for a battery-powered Flex device with LI_ION chemistry."""
    device_config = deepcopy(DEVICE_CONFIG)
    device_config.power = replace(
        device_config.power,
        power_mode=PowerMode.BATTERY,
        capacity_estimator=1,  # LI_ION
    )
    mock_opendisplay_device.config = device_config

    await setup_entry()

    inject_bluetooth_service_info(hass, VALID_SERVICE_INFO)
    await hass.async_block_till_done()

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_battery_sensors_not_created_for_usb_devices(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Test battery sensors are not created for USB-powered devices."""
    await setup_entry()

    inject_bluetooth_service_info(hass, VALID_SERVICE_INFO)
    await hass.async_block_till_done()

    assert entity_registry.async_get("sensor.opendisplay_1234_battery") is None
    assert entity_registry.async_get("sensor.opendisplay_1234_battery_voltage") is None


async def test_no_sensors_for_non_flex_devices(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
    entity_registry: er.EntityRegistry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Test which sensor entities a non-Flex device gets.

    Non-Flex tags still advertise, so the advertisement-derived sensors are
    created for them; only the Flex-specific hardware sensors are not.
    """
    mock_opendisplay_device.is_flex = False
    await setup_entry()

    assert (
        entity_registry.async_get("sensor.opendisplay_1234_chip_temperature")
        is not None
    )
    assert entity_registry.async_get("sensor.opendisplay_1234_signal_strength_rssi")
    assert entity_registry.async_get("sensor.opendisplay_1234_last_seen")

    # USB-powered, so still no battery sensors
    assert entity_registry.async_get("sensor.opendisplay_1234_battery") is None
    assert entity_registry.async_get("sensor.opendisplay_1234_battery_voltage") is None


async def test_coordinator_ignores_unknown_manufacturer(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Test that advertisements from an unknown manufacturer ID are ignored."""
    await setup_entry()

    unknown_service_info = make_service_info(
        address=TEST_ADDRESS,
        manufacturer_data={0x9999: b"\x00" * 14},
    )
    inject_bluetooth_service_info(hass, unknown_service_info)
    await hass.async_block_till_done()

    # Coordinator has no data; device is visible but no OpenDisplay data parsed
    assert (
        hass.states.get("sensor.opendisplay_1234_chip_temperature").state
        == STATE_UNKNOWN
    )


async def test_sensor_goes_unavailable_when_device_disappears(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Test that sensors become unavailable when the device stops advertising."""
    start_monotonic = time.monotonic()
    await setup_entry()

    inject_bluetooth_service_info(hass, VALID_SERVICE_INFO)
    await hass.async_block_till_done()

    assert (
        hass.states.get("sensor.opendisplay_1234_chip_temperature").state
        != STATE_UNAVAILABLE
    )

    # Must exceed both the connectable stale threshold (195s) and the
    # unavailability polling interval (300s) to trigger the callback.
    advance = (
        CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS
        + UNAVAILABLE_TRACK_SECONDS
        + 1
    )
    monotonic_now = start_monotonic + advance
    with (
        patch_bluetooth_time(monotonic_now),
        patch_all_discovered_devices([]),
    ):
        async_fire_time_changed(
            hass,
            dt_util.utcnow() + timedelta(seconds=advance),
        )
        await hass.async_block_till_done()

    assert (
        hass.states.get("sensor.opendisplay_1234_chip_temperature").state
        == STATE_UNAVAILABLE
    )


async def test_battery_sensor_defaults_to_liion_when_capacity_estimator_unset(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """Test battery % uses LI_ION when capacity_estimator is 0."""
    device_config = deepcopy(DEVICE_CONFIG)
    device_config.power = replace(
        device_config.power,
        power_mode=PowerMode.BATTERY,
        capacity_estimator=0,  # not configured — defaults to LI_ION in sensor.py
    )
    mock_opendisplay_device.config = device_config

    await setup_entry()
    inject_bluetooth_service_info(hass, VALID_SERVICE_INFO)
    await hass.async_block_till_done()

    battery_state = hass.states.get("sensor.opendisplay_1234_battery")
    assert battery_state is not None
    # capacity_estimator=0 should fall back to LI_ION, producing
    # the same value as explicit LI_ION
    expected = voltage_to_percent(3700, CapacityEstimator.LI_ION)
    assert battery_state.state == str(expected)


# --- SHT40 ambient sensors -------------------------------------------------


@pytest.mark.parametrize("device_config", [make_sht40_device_config()])
async def test_sht40_entities_created_and_report_the_reading(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """A device with an SHT40 gets ambient entities that decode the advertisement."""
    await setup_entry()

    inject_bluetooth_service_info(hass, make_sht40_service_info())
    await hass.async_block_till_done()

    assert hass.states.get("sensor.opendisplay_1234_temperature").state == "28.0"
    assert hass.states.get("sensor.opendisplay_1234_humidity").state == "63.6"


async def test_no_sht40_entities_without_a_configured_sensor(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """The default device config has no SHT40, so no ambient entities appear."""
    await setup_entry()

    assert entity_registry.async_get("sensor.opendisplay_1234_temperature") is None


@pytest.mark.parametrize("device_config", [make_sht40_device_config()])
@pytest.mark.parametrize(
    ("block", "reason"),
    [
        (b"\xff\xff\xff", "the firmware's read-failure sentinel"),
        (b"\x00\x00\x00", "an unwritten slot, not a real -40 C reading"),
    ],
)
async def test_sht40_unreadable_block_reports_unknown(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
    block: bytes,
    reason: str,
) -> None:
    """A sentinel or unwritten block is unknown rather than a bogus measurement."""
    await setup_entry()

    inject_bluetooth_service_info(hass, make_sht40_service_info(block=block))
    await hass.async_block_till_done()

    assert (
        hass.states.get("sensor.opendisplay_1234_temperature").state == STATE_UNKNOWN
    ), reason


@pytest.mark.parametrize(
    "device_config", [make_sht40_device_config(msd_data_start_byte=1)]
)
async def test_sht40_reads_from_the_configured_offset(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """E1001/E1002/E1004 place the block at 1, not the firmware default of 7."""
    await setup_entry()

    inject_bluetooth_service_info(hass, make_sht40_service_info(start_byte=1))
    await hass.async_block_till_done()

    assert hass.states.get("sensor.opendisplay_1234_temperature").state == "28.0"


@pytest.mark.parametrize(
    "device_config", [make_sht40_device_config(msd_data_start_byte=0)]
)
async def test_sht40_offset_zero_resolves_to_the_default_slot(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """0 means "use the default", not byte 0, so the reading is still found."""
    await setup_entry()

    inject_bluetooth_service_info(hass, make_sht40_service_info())
    await hass.async_block_till_done()

    assert hass.states.get("sensor.opendisplay_1234_temperature").state == "28.0"


def test_sht40_entities_are_primary_not_diagnostic() -> None:
    """Ambient readings are what the device is for; the chip temperature is not."""
    for description in _sht40_descriptions(make_sht40_device_config().sensors[0]):
        assert description.entity_category is None
        assert description.entity_registry_enabled_default is True


def test_chip_temperature_stays_diagnostic_and_disabled() -> None:
    """The chip temperature is a diagnostic, and off by default."""
    assert _TEMPERATURE_DESCRIPTION.entity_category is not None
    assert _TEMPERATURE_DESCRIPTION.entity_registry_enabled_default is False


def test_chip_temperature_keeps_its_unique_id_key() -> None:
    """Renaming is display-only: changing the key would orphan existing entities."""
    assert _TEMPERATURE_DESCRIPTION.key == "temperature"
    assert _TEMPERATURE_DESCRIPTION.translation_key == "chip_temperature"


def test_sht40_keys_are_distinct_per_instance() -> None:
    """A second SHT40 must not collide with the first one's unique_id."""
    first_sensor = make_sht40_device_config().sensors[0]
    second_sensor = make_sht40_device_config().sensors[0]
    second_sensor.instance_number = 1

    first = {d.key for d in _sht40_descriptions(first_sensor)}
    assert first.isdisjoint({d.key for d in _sht40_descriptions(second_sensor)})


# --- last_seen -------------------------------------------------------------


async def test_last_seen_converts_monotonic_to_wall_time(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """last_seen reports a tz-aware wall-clock time, not the monotonic reading.

    habluetooth stores advertisement times on the monotonic clock, which is an
    arbitrary epoch; rendering it directly would produce a nonsense timestamp.
    """
    await setup_entry()

    inject_bluetooth_service_info(hass, VALID_SERVICE_INFO)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.opendisplay_1234_last_seen")
    parsed = dt_util.parse_datetime(state.state)
    assert parsed is not None
    assert parsed.tzinfo is not None
    # Within a minute of now, i.e. wall clock rather than time.monotonic().
    assert abs((dt_util.utcnow() - parsed).total_seconds()) < 60


async def test_last_seen_unknown_before_any_advertisement(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """With nothing ever seen, last_seen has no value to report."""
    await setup_entry()

    assert (
        hass.states.get("sensor.opendisplay_1234_last_seen").state == STATE_UNAVAILABLE
    )


# --- resolution ------------------------------------------------------------


def _resolution_sensor(*displays: DisplayConfig) -> OpenDisplayResolutionSensor:
    """Return a resolution sensor over a runtime_data whose config can be swapped."""
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            device_config=SimpleNamespace(displays=list(displays))
        )
    )
    coordinator = MagicMock()
    coordinator.address = TEST_ADDRESS
    return OpenDisplayResolutionSensor(coordinator, _RESOLUTION_DESCRIPTION, entry)


def test_resolution_reports_the_panel_as_configured() -> None:
    """The state is the native geometry; the rest of the packet becomes attributes."""
    sensor = _resolution_sensor(DEVICE_CONFIG.displays[0])

    assert sensor.available
    assert sensor.native_value == "296x128"
    assert sensor.extra_state_attributes == {
        "pixel_width": 296,
        "pixel_height": 128,
        "active_width_mm": 67,
        "active_height_mm": 29,
        "color_scheme": "BWR",
        "rotation": 0,
    }


def test_unrecognised_values_do_not_masquerade_as_valid_ones() -> None:
    """Rotation is reported in degrees, so an unmapped index must not pass for one."""
    display = replace(DEVICE_CONFIG.displays[0], rotation=99, color_scheme=99)

    attrs = _resolution_sensor(display).extra_state_attributes

    assert attrs["rotation"] is None
    assert attrs["color_scheme"] == 99


def test_config_is_re_read_so_a_wake_time_resync_is_picked_up() -> None:
    """delivery.py replaces device_config wholesale; a cached display would go stale."""
    sensor = _resolution_sensor(DEVICE_CONFIG.displays[0])
    assert sensor.native_value == "296x128"

    sensor._entry.runtime_data.device_config = SimpleNamespace(
        displays=[replace(DEVICE_CONFIG.displays[0], pixel_width=960, pixel_height=640)]
    )

    assert sensor.native_value == "960x640"


def test_a_display_less_device_reports_nothing() -> None:
    """No display in the config means no geometry to report, not a zero-sized panel."""
    sensor = _resolution_sensor()

    assert not sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None
