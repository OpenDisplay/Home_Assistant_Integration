"""Unit tests for button event enumeration and tracker-event translation.

Translation scenarios are taken from live XTEINK X4 captures (two ADC
ladders plus a digital power button).
"""

from opendisplay.models import BinaryInputs
from opendisplay.models.advertisement import ButtonChangeEvent
from opendisplay.models.enums import BinaryInputType

from custom_components.opendisplay.event import _button_ids, _events_for_button

# --- helpers ---------------------------------------------------------------


def _digital(input_flags: int, instance: int = 0) -> BinaryInputs:
    return BinaryInputs(
        instance_number=instance,
        input_type=BinaryInputType.DIGITAL,
        display_as=1,
        reserved_pins=bytes([3]) + bytes(7),
        input_flags=input_flags,
        invert=input_flags,
        pullups=input_flags,
        pulldowns=0,
        button_data_byte_index=7,
        reserved=bytes(12),
    )


def _ladder(count: int, id_base: int) -> BinaryInputs:
    # thresholds are irrelevant for enumeration; any strictly-descending set works
    thresholds = list(range((count + 1) * 100, 0, -100))[: count + 1]
    thresholds[-1] = 0
    return BinaryInputs.adc_ladder(
        instance_number=0,
        adc_pin=1,
        id_base=id_base,
        button_data_byte_index=5,
        thresholds=thresholds,
    )


def _forged_ladder(reserved: bytes) -> BinaryInputs:
    return BinaryInputs(
        instance_number=0,
        input_type=BinaryInputType.ADC_LADDER,
        display_as=0,
        reserved_pins=bytes([1]) + bytes(7),
        input_flags=0,
        invert=0,
        pullups=0,
        pulldowns=0,
        button_data_byte_index=5,
        reserved=reserved,
    )


def _raw(button_id: int, count: int, pressed: bool) -> int:
    return (button_id & 0x07) | ((count & 0x0F) << 3) | (0x80 if pressed else 0)


def _event(
    event_type: str,
    button_id: int,
    pressed: bool,
    count: int,
    previous_raw: int,
    byte_index: int = 5,
) -> ButtonChangeEvent:
    return ButtonChangeEvent(
        address="AA:BB:CC:DD:EE:FF",
        byte_index=byte_index,
        event_type=event_type,
        button_id=button_id,
        pressed=pressed,
        press_count=count,
        previous_press_count=(previous_raw >> 3) & 0x0F,
        raw=_raw(button_id, count, pressed),
        previous_raw=previous_raw,
        timestamp=0.0,
    )


# --- _button_ids: enumeration ------------------------------------------------


def test_digital_block_enumerates_set_bits():
    assert _button_ids(_digital(0b0000_0001)) == [0]
    assert _button_ids(_digital(0b1010_0010)) == [1, 5, 7]
    assert _button_ids(_digital(0)) == []


def test_ladder_block_enumerates_id_range():
    # X4 ladder 1: 4 buttons, ids 0-3
    assert _button_ids(_ladder(4, 0)) == [0, 1, 2, 3]
    # X4 ladder 2: 2 buttons, ids 4-5
    assert _button_ids(_ladder(2, 4)) == [4, 5]


def test_ladder_block_ignores_input_flags_bitmask():
    ladder = _ladder(2, 4)
    assert ladder.input_flags == 0  # the bitmask is unused for ladders
    assert _button_ids(ladder) == [4, 5]


def test_ladder_rejects_bad_shapes():
    for count, id_base in [(0, 0), (5, 0), (4, 5), (2, 7)]:
        forged = _forged_ladder(bytes([count, id_base]) + bytes(10))
        assert _button_ids(forged) == []


def test_ladder_rejects_short_reserved():
    assert _button_ids(_forged_ladder(b"\x02")) == []


# --- _events_for_button: tracker-event translation ---------------------------


def test_down_up_pass_through_for_matching_id():
    down = _event("button_down", 2, True, 3, previous_raw=_raw(2, 2, False))
    up = _event("button_up", 2, False, 3, previous_raw=_raw(2, 3, True))
    assert _events_for_button(down, 2) == ["button_down"]
    assert _events_for_button(up, 2) == ["button_up"]
    assert _events_for_button(down, 1) == []
    assert _events_for_button(up, 3) == []


def test_slot_changed_fires_down_on_new_button():
    # Live capture: Back released (id 0), then Left pressed (id 2).
    ev = _event("button_slot_changed", 2, True, 3, previous_raw=_raw(0, 2, False))
    assert _events_for_button(ev, 2) == ["button_down"]
    assert _events_for_button(ev, 0) == []  # old button was already released


def test_slot_changed_releases_old_button_when_release_frame_lost():
    ev = _event("button_slot_changed", 5, True, 1, previous_raw=_raw(4, 1, True))
    assert _events_for_button(ev, 4) == ["button_up"]
    assert _events_for_button(ev, 5) == ["button_down"]


def test_slot_changed_with_release_only_frame_fires_full_press():
    # Live capture (VolUp): only the new button's release frame was seen.
    ev = _event("button_slot_changed", 4, False, 2, previous_raw=_raw(5, 1, False))
    assert _events_for_button(ev, 4) == ["button_down", "button_up"]


def test_press_count_changed_ignored_when_state_transition_reported():
    # button_down already fired for this frame pair; must not double-fire.
    ev = _event("press_count_changed", 2, True, 3, previous_raw=_raw(2, 2, False))
    assert _events_for_button(ev, 2) == []


def test_press_count_changed_recovers_missed_press():
    ev = _event("press_count_changed", 2, False, 4, previous_raw=_raw(2, 3, False))
    assert _events_for_button(ev, 2) == ["button_down", "button_up"]
    assert _events_for_button(ev, 1) == []


def test_press_count_changed_recovers_missed_release_and_repress():
    ev = _event("press_count_changed", 2, True, 4, previous_raw=_raw(2, 3, True))
    assert _events_for_button(ev, 2) == ["button_up", "button_down"]
