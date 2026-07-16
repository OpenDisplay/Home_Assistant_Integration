"""Unit tests for the image-service queue contract.

The service module's HA/BLE touchpoints are patched in its own namespace so the
tests do not need a full Home Assistant harness.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from opendisplay import DitherMode, RefreshMode
from PIL import Image as PILImage
import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.opendisplay import services as services_mod
from custom_components.opendisplay.delivery import DeliveryReceipt
from custom_components.opendisplay.services import _async_send_image
from custom_components.opendisplay.sleep import SleepProfile

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _profile(**overrides):
    params = {
        "sleep_mode": "on",
        "power_mode": 1,
        "sleep_timeout_ms": 0,
        "deep_sleep_time_seconds": 300,
        "missed_cycles": 3,
        "queue_timeout_hours": 24,
    }
    params.update(overrides)
    return SleepProfile.create(**params)


def _make_env(profile=None, last_seen=None, manager=None):
    """Return (hass, entry, coordinator, manager) with a fake runtime."""
    profile = profile or _profile()
    coordinator = MagicMock()
    coordinator.data = SimpleNamespace(last_seen=last_seen)
    if manager is None:
        manager = MagicMock()
        manager.submit_upload = MagicMock(
            return_value=DeliveryReceipt(status="queued", expires_at=123.0)
        )
        manager.notify_device_seen = MagicMock()
    runtime = SimpleNamespace(
        coordinator=coordinator,
        sleep_profile=profile,
        delivery=manager,
        device_config=None,
        ble_lock=asyncio.Lock(),
        partial_state=MagicMock(),
    )
    entry = MagicMock()
    entry.unique_id = ADDRESS
    entry.runtime_data = runtime
    entry.data = {}  # no encryption key
    entry.options = {}  # no custom transfer options
    hass = MagicMock()

    async def _executor_job(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = _executor_job
    return hass, entry, coordinator, manager


def _send(hass, entry):
    img = PILImage.new("RGB", (1, 1))
    return _async_send_image(
        hass,
        entry,
        img,
        dither_mode=DitherMode.NONE,
        refresh_mode=RefreshMode.FULL,
    )


def _patches():
    """Common patch set for a _async_send_image drive."""
    return (
        patch.object(
            services_mod, "prepare_image", return_value=(b"img", None, object())
        ),
        patch.object(services_mod, "_pil_to_jpeg", return_value=b"jpeg"),
        patch.object(services_mod, "OpenDisplayDevice"),
    )


@pytest.mark.asyncio
async def test_image_send_always_queues_and_does_not_open_live_connection():
    hass, entry, _, manager = _make_env(last_seen=None)
    p1, p2, p3 = _patches()
    with p1, p2, p3 as od:
        receipt = await _send(hass, entry)

    assert receipt.status == "queued"
    manager.submit_upload.assert_called_once()
    od.assert_not_called()


@pytest.mark.asyncio
async def test_queue_submission_threads_prepared_frame_state_and_refresh_mode():
    hass, entry, _, manager = _make_env(last_seen=None)
    p1, p2, p3 = _patches()
    with p1, p2, p3:
        await _send(hass, entry)

    kwargs = manager.submit_upload.call_args.kwargs
    assert kwargs["prepared"][0] == b"img"
    assert kwargs["refresh_mode"] is RefreshMode.FULL
    assert kwargs["partial_state"] is entry.runtime_data.partial_state
    assert kwargs["use_measured_palettes"] is False
    assert kwargs["preview_jpeg"] == b"jpeg"


@pytest.mark.asyncio
async def test_fresh_sleepy_device_kicks_background_drain():
    hass, entry, _, manager = _make_env(last_seen=time.time())
    p1, p2, p3 = _patches()
    with p1, p2, p3:
        receipt = await _send(hass, entry)

    assert receipt.status == "queued"
    manager.notify_device_seen.assert_called_once_with("queued-submit")


@pytest.mark.asyncio
async def test_stale_sleepy_device_still_kicks_background_drain():
    hass, entry, _, manager = _make_env(last_seen=None)
    p1, p2, p3 = _patches()
    with p1, p2, p3:
        receipt = await _send(hass, entry)

    assert receipt.status == "queued"
    manager.notify_device_seen.assert_called_once_with("queued-submit")


@pytest.mark.asyncio
async def test_image_send_does_not_check_ble_reachability():
    hass, entry, _, manager = _make_env(last_seen=time.time())
    p1, p2, p3 = _patches()
    with (
        p1,
        p2,
        p3,
        patch.object(services_mod, "async_ble_device_from_address") as ble_lookup,
    ):
        receipt = await _send(hass, entry)

    assert receipt.status == "queued"
    ble_lookup.assert_not_called()
    manager.notify_device_seen.assert_called_once_with("queued-submit")


@pytest.mark.asyncio
async def test_not_sleepy_device_kicks_background_drain_when_connectable():
    hass, entry, _, manager = _make_env(
        profile=_profile(sleep_mode="off"), last_seen=None
    )
    p1, p2, p3 = _patches()
    with p1, p2, p3:
        receipt = await _send(hass, entry)

    assert receipt.status == "queued"
    manager.notify_device_seen.assert_called_once_with("queued-submit")


@pytest.mark.asyncio
async def test_missing_delivery_manager_raises_upload_error():
    hass, entry, _, _ = _make_env(manager=None)
    entry.runtime_data.delivery = None
    p1, p2, p3 = _patches()
    with p1, p2, p3, pytest.raises(HomeAssistantError):
        await _send(hass, entry)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
