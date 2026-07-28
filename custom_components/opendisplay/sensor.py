"""Sensor platform for OpenDisplay devices."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import time

from homeassistant.components.bluetooth import async_last_service_info
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from opendisplay import voltage_to_percent
from opendisplay.models.advertisement import Sht40Reading
from opendisplay.models.config import DisplayConfig, SensorData
from opendisplay.models.enums import CapacityEstimator, PowerMode, Rotation, SensorType

from . import OpenDisplayConfigEntry
from .coordinator import OpenDisplayCoordinator, OpenDisplayUpdate
from .entity import OpenDisplayEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class OpenDisplaySensorEntityDescription(SensorEntityDescription):
    """Describes an OpenDisplay sensor entity."""

    value_fn: (
        Callable[[OpenDisplayUpdate], float | int | str | datetime | None] | None
    ) = None


# The MCU's own temperature, not an attached sensor. translation_key only sets
# the display name; the key -- and so the unique_id -- stays "temperature", so
# entities that already exist keep their history.
_TEMPERATURE_DESCRIPTION = OpenDisplaySensorEntityDescription(
    key="temperature",
    translation_key="chip_temperature",
    device_class=SensorDeviceClass.TEMPERATURE,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    value_fn=lambda upd: upd.advertisement.temperature_c,
)


def _sht40_descriptions(
    sensor: SensorData,
) -> list[OpenDisplaySensorEntityDescription]:
    """Build ambient temperature and humidity entities for one SHT40.

    The reading rides in the advertisement, so these need no connection. Its
    offset within the dynamic block is per-board and cannot be assumed --
    reTerminal E1001/E1002/E1004 use 1 while the firmware default is 7 -- so it
    comes from the device's own config and is captured once per entity here.

    Unlike the chip temperature these are primary entities: not diagnostic, and
    enabled by default.
    """
    start_byte = sensor.sht40_msd_start_byte

    def _reading(upd: OpenDisplayUpdate) -> Sht40Reading | None:
        return upd.advertisement.sht40_reading(start_byte)

    def _temperature(upd: OpenDisplayUpdate) -> float | None:
        reading = _reading(upd)
        return None if reading is None else reading.temperature_c

    def _humidity(upd: OpenDisplayUpdate) -> float | None:
        reading = _reading(upd)
        return None if reading is None else reading.humidity_percent

    return [
        OpenDisplaySensorEntityDescription(
            key=f"sht40_{sensor.instance_number}_temperature",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=_temperature,
        ),
        OpenDisplaySensorEntityDescription(
            key=f"sht40_{sensor.instance_number}_humidity",
            device_class=SensorDeviceClass.HUMIDITY,
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=_humidity,
        ),
    ]


_BATTERY_POWER_MODES = {PowerMode.BATTERY, PowerMode.SOLAR}

_BATTERY_VOLTAGE_DESCRIPTION = OpenDisplaySensorEntityDescription(
    key="battery_voltage",
    translation_key="battery_voltage",
    device_class=SensorDeviceClass.VOLTAGE,
    native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    value_fn=lambda upd: upd.advertisement.battery_mv,
)

_RSSI_DESCRIPTION = OpenDisplaySensorEntityDescription(
    key="rssi",
    translation_key="rssi",
    device_class=SensorDeviceClass.SIGNAL_STRENGTH,
    native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    value_fn=lambda upd: upd.rssi,
)

_LAST_SEEN_DESCRIPTION = OpenDisplaySensorEntityDescription(
    key="last_seen",
    translation_key="last_seen",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
)

_RESOLUTION_DESCRIPTION = OpenDisplaySensorEntityDescription(
    key="resolution",
    translation_key="resolution",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up OpenDisplay sensor entities."""
    coordinator = entry.runtime_data.coordinator
    device_config = entry.runtime_data.device_config
    power_config = device_config.power
    descriptions: list[OpenDisplaySensorEntityDescription] = [
        _TEMPERATURE_DESCRIPTION,
        _RSSI_DESCRIPTION,
        _LAST_SEEN_DESCRIPTION,
    ]

    for sensor in device_config.sensors:
        if sensor.sensor_type_enum is SensorType.SHT40:
            descriptions += _sht40_descriptions(sensor)

    if power_config.power_mode_enum in _BATTERY_POWER_MODES:
        capacity_estimator = power_config.capacity_estimator or CapacityEstimator.LI_ION
        descriptions += [
            _BATTERY_VOLTAGE_DESCRIPTION,
            OpenDisplaySensorEntityDescription(
                key="battery",
                device_class=SensorDeviceClass.BATTERY,
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda upd: voltage_to_percent(
                    upd.advertisement.battery_mv, capacity_estimator
                ),
            ),
        ]

    entities: list[OpenDisplaySensorEntity] = [
        (
            OpenDisplayLastSeenSensor(coordinator, description)
            if description.key == "last_seen"
            else OpenDisplaySensorEntity(coordinator, description)
        )
        for description in descriptions
    ]

    if device_config.displays:
        entities.append(
            OpenDisplayResolutionSensor(coordinator, _RESOLUTION_DESCRIPTION, entry)
        )

    async_add_entities(entities)


class OpenDisplaySensorEntity(OpenDisplayEntity, SensorEntity):
    """A sensor entity for an OpenDisplay device."""

    entity_description: OpenDisplaySensorEntityDescription

    @property
    def native_value(self) -> float | int | str | datetime | None:
        """Return the sensor value."""
        value_fn = self.entity_description.value_fn
        if value_fn is None or self.coordinator.data is None:
            return None
        return value_fn(self.coordinator.data)


class OpenDisplayLastSeenSensor(OpenDisplaySensorEntity):
    """last_seen sourced from the bluetooth stack, not the gated callback."""

    @property
    def native_value(self) -> datetime | None:
        """Return the last time the bluetooth stack saw an advertisement."""
        # connectable=False matches the Bluetooth advertisement monitor's
        # "updated" field: _all_history, refreshed on every received advert
        # before core's connectable/de-dup gates.
        info = async_last_service_info(
            self.hass, self.coordinator.address, connectable=False
        )
        if info is None:
            return None
        # info.time is a monotonic clock (monotonic_time_coarse); convert to
        # wall time with the same offset the advertisement monitor uses.
        wall = info.time + (time.time() - time.monotonic())
        return datetime.fromtimestamp(wall, tz=UTC)


class OpenDisplayResolutionSensor(OpenDisplaySensorEntity):
    """A panel's native resolution, with its physical size and colour scheme."""

    def __init__(
        self,
        coordinator: OpenDisplayCoordinator,
        description: OpenDisplaySensorEntityDescription,
        entry: OpenDisplayConfigEntry,
    ) -> None:
        """Initialize against the config entry whose display is reported."""
        super().__init__(coordinator, description)
        self._entry = entry

    @property
    def _display(self) -> DisplayConfig | None:
        """Return the display config, re-read since a resync replaces it."""
        displays = self._entry.runtime_data.device_config.displays
        return displays[0] if displays else None

    @property
    def available(self) -> bool:
        """Return True while the display is in the config, awake or not."""
        return self._display is not None

    @property
    def native_value(self) -> str | None:
        """Return the native resolution as ``WIDTHxHEIGHT``."""
        display = self._display
        if display is None:
            return None
        return f"{display.pixel_width}x{display.pixel_height}"

    @property
    def extra_state_attributes(self) -> dict[str, int | str | None] | None:
        """Return the panel's pixel and physical dimensions, scheme and rotation."""
        display = self._display
        if display is None:
            return None
        color_scheme = display.color_scheme_enum
        rotation = display.rotation_enum
        return {
            "pixel_width": display.pixel_width,
            "pixel_height": display.pixel_height,
            "active_width_mm": display.active_width_mm,
            "active_height_mm": display.active_height_mm,
            "color_scheme": (
                color_scheme if isinstance(color_scheme, int) else color_scheme.name
            ),
            "rotation": int(rotation) if isinstance(rotation, Rotation) else None,
        }
