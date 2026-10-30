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
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
import homeassistant.util.dt as dt_util

from opendisplay import voltage_to_percent
from opendisplay.models.advertisement import Sht40Reading
from opendisplay.models.config import SensorData
from opendisplay.models.enums import CapacityEstimator, PowerMode, SensorType

from . import OpenDisplayConfigEntry
from .coordinator import OpenDisplayUpdate
from .entity import OpenDisplayEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class OpenDisplaySensorEntityDescription(SensorEntityDescription):
    """Describes an OpenDisplay sensor entity."""

    value_fn: Callable[[OpenDisplayUpdate], float | int | str | datetime | None]


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
    # native_value is overridden by OpenDisplayLastSeenSensorEntity, so this value_fn
    # is dead code; value_fn is a required field, hence the no-op.
    value_fn=lambda _upd: None,
)


def _entity_for_description(
    coordinator, description: OpenDisplaySensorEntityDescription
) -> OpenDisplaySensorEntity:
    """Pick the entity class for a sensor description."""
    
    if description.key == "last_seen":
        return OpenDisplayLastSeenSensorEntity(coordinator, description)
    # Battery readings only ride in the advertisement, so they update far less
    # often than the device's own wake cadence. Keep showing the last known
    # value (and stay available) across Bluetooth gaps instead of flashing
    # unavailable.
    if description.key in {"battery", "battery_voltage", "rssi", "temperature"}:
        return OpenDisplayStickySensorEntity(coordinator, description)
    return OpenDisplaySensorEntity(coordinator, description)


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

    async_add_entities(
        _entity_for_description(coordinator, description)
        for description in descriptions
    )


class OpenDisplaySensorEntity(OpenDisplayEntity, SensorEntity):
    """A sensor entity for an OpenDisplay device."""

    entity_description: OpenDisplaySensorEntityDescription

    @property
    def native_value(self) -> float | int | str | datetime | None:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class OpenDisplayStickySensorEntity(OpenDisplaySensorEntity, RestoreEntity):
    """A sensor that keeps its last known value once observed.
    
    Some readings (e.g. battery) only ride in the advertisement and can go
    a long time between updates. Rather than flashing "unavailable" whenever
    Bluetooth briefly loses the device, cache the last non-None value and
    stay available.
    """
    
    def __init__(
        self,
        coordinator,
        description: OpenDisplaySensorEntityDescription,
    ) -> None:
        """Initialize the sticky sensor."""
        super().__init__(coordinator, description)
        self._last_value: float | int | str | datetime | None = None
    
    @property
    def available(self) -> bool:
        """Stay available once a value has been observed."""
        return self._last_value is not None or super().available
    
    @property
    def native_value(self) -> float | int | str | datetime | None:
        """Return the last known value, updating it if a fresh one is present."""
        if self.coordinator.data is not None:
            value = self.entity_description.value_fn(self.coordinator.data)
            if value is not None:
                self._last_value = value
        return self._last_value

    async def async_added_to_hass(self) -> None:
        """Restore the previous value until Bluetooth has one."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is None:
            return
        if last_state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            return
        if self.device_class == SensorDeviceClass.TIMESTAMP:
            self._last_value = dt_util.parse_datetime(last_state.state)
        else:
            self._last_value = last_state.state


class OpenDisplayLastSeenSensorEntity(OpenDisplayStickySensorEntity):
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
            return self._last_value
        # info.time is a monotonic clock (monotonic_time_coarse); convert to
        # wall time with the same offset the advertisement monitor uses.
        wall = info.time + (time.time() - time.monotonic())
        self._last_value = datetime.fromtimestamp(wall, tz=UTC)
        return self._last_value
