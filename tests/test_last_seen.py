"""Unit tests for the last_seen sensor with a mocked bluetooth stack.

These avoid the full Home Assistant test harness: ``async_last_service_info``
(the sensor's only HA touchpoint) is patched in the sensor module namespace and
the entity's ``native_value`` property is asserted directly.
"""

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.opendisplay import sensor as sensor_mod
from custom_components.opendisplay.sensor import (
    _LAST_SEEN_DESCRIPTION,
    OpenDisplayLastSeenSensor,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _make_sensor():
    """Return an OpenDisplayLastSeenSensor wired to mock coordinator/hass."""
    coordinator = MagicMock()
    coordinator.address = ADDRESS
    entity = OpenDisplayLastSeenSensor(coordinator, _LAST_SEEN_DESCRIPTION)
    entity.hass = MagicMock()
    return entity


def test_native_value_converts_monotonic_to_wall_time():
    entity = _make_sensor()
    # info.time is a monotonic-clock reading, like habluetooth's _all_history.
    mono = time.monotonic()
    with patch.object(
        sensor_mod,
        "async_last_service_info",
        return_value=SimpleNamespace(time=mono),
    ) as mock_info:
        value = entity.native_value

    # A monotonic "now" maps to wall-clock "now"; must be tz-aware for TIMESTAMP.
    assert isinstance(value, datetime)
    assert value.tzinfo is not None
    expected = datetime.now(tz=timezone.utc)
    assert abs((value - expected).total_seconds()) < 2

    # The stack is queried with connectable=False to match the advertisement
    # monitor's "updated" column (pre-gate _all_history), and for the coordinator
    # address.
    mock_info.assert_called_once()
    assert mock_info.call_args.kwargs["connectable"] is False
    assert ADDRESS in mock_info.call_args.args


def test_native_value_none_when_no_service_info():
    entity = _make_sensor()
    with patch.object(sensor_mod, "async_last_service_info", return_value=None):
        assert entity.native_value is None


def test_native_value_keeps_previous_timestamp_when_service_info_disappears():
    entity = _make_sensor()
    mono = time.monotonic()
    with patch.object(
        sensor_mod,
        "async_last_service_info",
        return_value=SimpleNamespace(time=mono),
    ):
        first = entity.native_value

    with patch.object(sensor_mod, "async_last_service_info", return_value=None):
        assert entity.native_value == first


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
