"""Event platform for OpenDisplay devices — button press/release and touch events."""

from dataclasses import dataclass

from opendisplay.models.advertisement import TouchTracker

from homeassistant.components.event import (
    EventDeviceClass,
    EventEntity,
    EventEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenDisplayConfigEntry
from .entity import OpenDisplayEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class OpenDisplayEventEntityDescription(EventEntityDescription):
    """Describes an OpenDisplay button event entity."""

    byte_index: int
    button_id: int


@dataclass(frozen=True, kw_only=True)
class OpenDisplayTouchEntityDescription(EventEntityDescription):
    """Describes an OpenDisplay touch event entity."""

    instance: int


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up OpenDisplay event entities from binary and touch controller config."""
    coordinator = entry.runtime_data.coordinator
    entity_registry = er.async_get(hass)

    def _remove_stale(prefix: str, active_ids: set[str]) -> None:
        for entity_entry in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        ):
            if (
                entity_entry.domain == "event"
                and entity_entry.unique_id.startswith(prefix)
                and entity_entry.unique_id not in active_ids
            ):
                entity_registry.async_remove(entity_entry.entity_id)

    # --- Button entities ---
    button_descriptions: list[OpenDisplayEventEntityDescription] = []
    button_number = 0
    for bi in entry.runtime_data.device_config.binary_inputs:
        for button_id in range(8):  # input_flags is a bitmask over 8 pin slots
            if bi.input_flags & (1 << button_id):
                button_number += 1
                button_descriptions.append(
                    OpenDisplayEventEntityDescription(
                        key=f"button_{bi.instance_number}_{button_id}",
                        translation_key="button",
                        translation_placeholders={"number": str(button_number)},
                        device_class=EventDeviceClass.BUTTON,
                        event_types=["button_down", "button_up"],
                        byte_index=bi.button_data_byte_index,
                        button_id=button_id,
                    )
                )

    _remove_stale(
        f"{coordinator.address}-button_",
        {f"{coordinator.address}-{d.key}" for d in button_descriptions},
    )

    # --- Touch entities ---
    touch_descriptions: list[OpenDisplayTouchEntityDescription] = []
    touch_trackers: list[TouchTracker] = []
    for number, tc in enumerate(entry.runtime_data.device_config.touch_controllers, 1):
        touch_descriptions.append(
            OpenDisplayTouchEntityDescription(
                key=f"touch_{tc.instance_number}",
                translation_key="touch",
                translation_placeholders={"number": str(number)},
                event_types=["touch_down", "touch_move", "touch_up"],
                instance=tc.instance_number,
                icon="mdi:gesture-tap",
            )
        )
        touch_trackers.append(
            TouchTracker(tc.instance_number, tc.touch_data_start_byte)
        )

    coordinator.touch_trackers = touch_trackers

    _remove_stale(
        f"{coordinator.address}-touch_",
        {f"{coordinator.address}-{d.key}" for d in touch_descriptions},
    )

    async_add_entities(
        OpenDisplayEventEntity(coordinator, description)
        for description in button_descriptions
    )
    async_add_entities(
        OpenDisplayTouchEventEntity(coordinator, description)
        for description in touch_descriptions
    )


class OpenDisplayEventEntity(
    OpenDisplayEntity[OpenDisplayEventEntityDescription], EventEntity
):
    """A button event entity for an OpenDisplay device."""

    _last_processed_data: object | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire events for button transitions reported by this coordinator update."""
        data = self.coordinator.data
        if data is not None and data is not self._last_processed_data:
            for event in data.button_events:
                if (
                    event.byte_index == self.entity_description.byte_index
                    and event.button_id == self.entity_description.button_id
                    and event.event_type in self.event_types
                ):
                    self._trigger_event(event.event_type)
            self._last_processed_data = data
            self.async_write_ha_state()


class OpenDisplayTouchEventEntity(
    OpenDisplayEntity[OpenDisplayTouchEntityDescription], EventEntity
):
    """A touch event entity for an OpenDisplay device."""

    _last_processed_data: object | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire events for touch transitions reported by this coordinator update."""
        data = self.coordinator.data
        if data is not None and data is not self._last_processed_data:
            for event in data.touch_events:
                if event.instance == self.entity_description.instance:
                    self._trigger_event(
                        event.event_type,
                        {"x": event.x, "y": event.y},
                    )
            self._last_processed_data = data
            self.async_write_ha_state()
