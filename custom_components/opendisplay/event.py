"""Event platform for OpenDisplay devices — button press/release and touch events."""

from dataclasses import dataclass

from opendisplay.models import BinaryInputs
from opendisplay.models.advertisement import ButtonChangeEvent, TouchTracker
from opendisplay.models.enums import BinaryInputType

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


def _button_ids(bi: BinaryInputs) -> list[int]:
    """Return the button ids reported by one binary_inputs block.

    Digital blocks: one button per set ``input_flags`` bit. Ladder blocks
    share one ADC pin and report ids id_base..id_base+count-1, with count
    and id_base at the start of the reserved tail.
    """
    if bi.input_type == BinaryInputType.ADC_LADDER:
        if len(bi.reserved) < 2:
            return []
        count = bi.reserved[0]
        id_base = bi.reserved[1]
        if not 1 <= count <= BinaryInputs.MAX_LADDER_BUTTONS:
            return []
        if id_base + count > BinaryInputs.MAX_BUTTON_ID + 1:
            return []
        return list(range(id_base, id_base + count))
    return [i for i in range(8) if bi.input_flags & (1 << i)]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up OpenDisplay event entities from binary_inputs and touch_controllers config."""
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

    def _add_button(bi, button_id: int) -> None:
        nonlocal button_number
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

    for bi in entry.runtime_data.device_config.binary_inputs:
        for button_id in _button_ids(bi):
            _add_button(bi, button_id)

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
        touch_trackers.append(TouchTracker(tc.instance_number, tc.touch_data_start_byte))

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


def _events_for_button(event: ButtonChangeEvent, button_id: int) -> list[str]:
    """Translate one tracker transition into down/up events for one button.

    Ladder buttons share a report byte: pressing a different button arrives
    as ``button_slot_changed``, and a press whose frames were dropped by BLE
    sampling arrives only as ``press_count_changed``.
    """
    if event.event_type in ("button_down", "button_up"):
        return [event.event_type] if event.button_id == button_id else []

    if event.event_type == "button_slot_changed":
        fired: list[str] = []
        previous_id = event.previous_raw & 0x07
        previous_pressed = bool(event.previous_raw & 0x80)
        if previous_id == button_id and previous_pressed:
            fired.append("button_up")  # release frame of the old button was lost
        if event.button_id == button_id:
            fired.append("button_down")
            if not event.pressed:
                fired.append("button_up")  # press and release both in one hop
        return fired

    if event.event_type == "press_count_changed":
        # A normal press emits this alongside button_down; only an unchanged
        # pressed state means presses were dropped between advertisements.
        if event.button_id != button_id:
            return []
        previous_pressed = bool(event.previous_raw & 0x80)
        if previous_pressed != event.pressed:
            return []  # the down/up transition was already reported
        if event.pressed:
            return ["button_up", "button_down"]  # missed release + re-press
        return ["button_down", "button_up"]  # missed a full press

    return []


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
                if event.byte_index != self.entity_description.byte_index:
                    continue
                for event_type in _events_for_button(
                    event, self.entity_description.button_id
                ):
                    self._trigger_event(event_type)
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
