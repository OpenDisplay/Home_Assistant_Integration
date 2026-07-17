"""Unit tests for the _async_send_image sleep gate and probe-before-queue.

Mirrors the test_delivery.py approach: no Home Assistant test harness; the
service module's HA/BLE touchpoints (``prepare_image``, ``_pil_to_jpeg``,
``async_dispatcher_send``, ``async_ble_device_from_address`` and
``OpenDisplayDevice``) are patched in the services module namespace.
"""

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from opendisplay import BLEConnectionError, RefreshMode, DitherMode
from PIL import Image as PILImage
import pytest

from custom_components.opendisplay import services as services_mod
from custom_components.opendisplay.ble_lock import ble_connection
from custom_components.opendisplay.const import (
    CONF_BLOCKS_PER_ACK,
    CONF_MAX_QUEUE_SIZE,
    DEFAULT_BLOCKS_PER_ACK,
    DEFAULT_MAX_QUEUE_SIZE,
)
from custom_components.opendisplay.delivery import DeliveryReceipt
from custom_components.opendisplay.services import (
    PROBE_CONNECT_TIMEOUT_S,
    PROBE_MAX_ATTEMPTS,
    _async_send_image,
)
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


def _make_env(profile=None, last_seen=None, options=None):
    """Return (hass, entry, coordinator, manager) with a fake runtime."""
    profile = profile or _profile()
    coordinator = MagicMock()
    coordinator.data = SimpleNamespace(last_seen=last_seen)
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
        partial_state=MagicMock(),
    )
    entry = MagicMock()
    entry.unique_id = ADDRESS
    entry.runtime_data = runtime
    entry.data = {}  # no encryption key
    entry.options = options if options is not None else {}
    hass = MagicMock()

    async def _executor_job(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = _executor_job
    return hass, entry, coordinator, manager


def _device_ctx_factory(device=None, exc=None, on_enter=None):
    """MagicMock construct-recording factory for OpenDisplayDevice.

    Produces an async context manager that yields ``device``, or raises
    ``exc`` from ``__aenter__`` (after calling ``on_enter`` if given).
    """

    class _Ctx:
        async def __aenter__(self):
            if on_enter is not None:
                on_enter()
            if exc is not None:
                raise exc
            return device

        async def __aexit__(self, *exc_info):
            return False

    return MagicMock(side_effect=lambda **kwargs: _Ctx())


def _send(hass, entry):
    img = PILImage.new("RGB", (1, 1))
    return _async_send_image(
        hass,
        entry,
        img,
        dither_mode=DitherMode.NONE,
        refresh_mode=RefreshMode.FULL,
    )


def _patches(od_factory, ble_device="ble-device"):
    """Common patch set for a _async_send_image drive."""
    return (
        patch.object(
            services_mod, "prepare_image", return_value=(b"img", None, object())
        ),
        patch.object(services_mod, "_pil_to_jpeg", return_value=b"jpeg"),
        patch.object(services_mod, "async_dispatcher_send"),
        patch.object(
            services_mod, "async_ble_device_from_address", return_value=ble_device
        ),
        patch.object(services_mod, "OpenDisplayDevice", od_factory),
    )


@pytest.mark.asyncio
async def test_probe_success_delivers():
    """Stale device + probe on: one reduced-budget attempt that succeeds."""
    hass, entry, _, manager = _make_env(last_seen=None)  # never seen -> asleep
    device = MagicMock()
    device.upload_prepared_image = AsyncMock()
    od = _device_ctx_factory(device=device)
    p1, p2, p3, p4, p5 = _patches(od)
    with p1, p2, p3, p4, p5:
        receipt = await _send(hass, entry)

    assert receipt.status == "delivered"
    od.assert_called_once()
    kwargs = od.call_args.kwargs
    assert kwargs["timeout"] == PROBE_CONNECT_TIMEOUT_S
    assert kwargs["max_attempts"] == PROBE_MAX_ATTEMPTS
    manager.submit_upload.assert_not_called()


@pytest.mark.asyncio
async def test_probe_failure_queues():
    """Stale device + probe on: the failed attempt falls back to the queue."""
    hass, entry, _, manager = _make_env(last_seen=None)
    od = _device_ctx_factory(exc=BLEConnectionError("dark"))
    p1, p2, p3, p4, p5 = _patches(od)
    with p1, p2, p3, p4, p5:
        receipt = await _send(hass, entry)

    assert receipt.status == "queued"
    od.assert_called_once()
    manager.submit_upload.assert_called_once()


@pytest.mark.asyncio
async def test_probe_disabled_queues_immediately():
    """probe_before_queue=False restores the old queue-without-connect gate."""
    hass, entry, _, manager = _make_env(
        profile=_profile(probe_before_queue=False), last_seen=None
    )
    od = _device_ctx_factory()
    p1, p2, p3, p4, p5 = _patches(od)
    with p1, p2, p3, p4 as ble_lookup, p5:
        receipt = await _send(hass, entry)

    assert receipt.status == "queued"
    ble_lookup.assert_not_called()
    od.assert_not_called()
    manager.submit_upload.assert_called_once()


@pytest.mark.asyncio
async def test_not_sleepy_full_budget():
    """Non-sleepy devices keep the library's default connect budget."""
    hass, entry, _, manager = _make_env(
        profile=_profile(sleep_mode="off"), last_seen=None
    )
    device = MagicMock()
    device.upload_prepared_image = AsyncMock()
    od = _device_ctx_factory(device=device)
    p1, p2, p3, p4, p5 = _patches(od)
    with p1, p2, p3, p4, p5:
        receipt = await _send(hass, entry)

    assert receipt.status == "delivered"
    kwargs = od.call_args.kwargs
    assert "timeout" not in kwargs
    assert "max_attempts" not in kwargs
    manager.submit_upload.assert_not_called()


@pytest.mark.asyncio
async def test_probe_no_ble_device_queues_cheaply():
    """No connectable BLEDevice: the probe short-circuits to the queue."""
    hass, entry, _, manager = _make_env(last_seen=None)
    od = _device_ctx_factory()
    p1, p2, p3, p4, p5 = _patches(od, ble_device=None)
    with p1, p2, p3, p4, p5:
        receipt = await _send(hass, entry)

    assert receipt.status == "queued"
    od.assert_not_called()
    manager.submit_upload.assert_called_once()


@pytest.mark.asyncio
async def test_post_probe_fresh_advert_triggers_drain():
    """A wake advert landing during the failed probe kicks an immediate drain."""
    hass, entry, coordinator, manager = _make_env(last_seen=None)

    def _advert_arrives():
        coordinator.data = SimpleNamespace(last_seen=time.time())

    od = _device_ctx_factory(
        exc=BLEConnectionError("dropped"), on_enter=_advert_arrives
    )
    p1, p2, p3, p4, p5 = _patches(od)
    with p1, p2, p3, p4, p5:
        receipt = await _send(hass, entry)

    assert receipt.status == "queued"
    manager.notify_device_seen.assert_called_once_with("post-probe")


@pytest.mark.asyncio
async def test_post_probe_still_stale_no_drain_kick():
    """No advert during the failed probe: wait for the natural next wake."""
    hass, entry, _, manager = _make_env(last_seen=None)
    od = _device_ctx_factory(exc=BLEConnectionError("dark"))
    p1, p2, p3, p4, p5 = _patches(od)
    with p1, p2, p3, p4, p5:
        receipt = await _send(hass, entry)

    assert receipt.status == "queued"
    manager.notify_device_seen.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_device_failure_no_probe_kwargs():
    """Sleepy but recently seen: full budget, queue on failure, no drain kick."""
    hass, entry, _, manager = _make_env(last_seen=time.time())
    od = _device_ctx_factory(exc=BLEConnectionError("dropped"))
    p1, p2, p3, p4, p5 = _patches(od)
    with p1, p2, p3, p4, p5:
        receipt = await _send(hass, entry)

    assert receipt.status == "queued"
    kwargs = od.call_args.kwargs
    assert "timeout" not in kwargs
    assert "max_attempts" not in kwargs
    manager.notify_device_seen.assert_not_called()


@pytest.mark.asyncio
async def test_pipe_kwargs_default():
    """No options set: library sliding-window defaults are threaded through."""
    hass, entry, _, manager = _make_env(
        profile=_profile(sleep_mode="off"), last_seen=None
    )
    device = MagicMock()
    device.upload_prepared_image = AsyncMock()
    od = _device_ctx_factory(device=device)
    p1, p2, p3, p4, p5 = _patches(od)
    with p1, p2, p3, p4, p5:
        receipt = await _send(hass, entry)

    assert receipt.status == "delivered"
    kwargs = od.call_args.kwargs
    assert kwargs["blocks_per_ack"] == DEFAULT_BLOCKS_PER_ACK
    assert kwargs["max_queue_size"] == DEFAULT_MAX_QUEUE_SIZE
    manager.submit_upload.assert_not_called()


@pytest.mark.asyncio
async def test_pipe_kwargs_custom():
    """Configured options reach the OpenDisplayDevice constructor."""
    hass, entry, _, _ = _make_env(
        profile=_profile(sleep_mode="off"),
        last_seen=None,
        options={CONF_BLOCKS_PER_ACK: 4, CONF_MAX_QUEUE_SIZE: 1},
    )
    device = MagicMock()
    device.upload_prepared_image = AsyncMock()
    od = _device_ctx_factory(device=device)
    p1, p2, p3, p4, p5 = _patches(od)
    with p1, p2, p3, p4, p5:
        receipt = await _send(hass, entry)

    assert receipt.status == "delivered"
    kwargs = od.call_args.kwargs
    assert kwargs["blocks_per_ack"] == 4
    assert kwargs["max_queue_size"] == 1


@pytest.mark.asyncio
async def test_live_send_passes_state_and_refresh_mode():
    """Live-send path pins the pipe-partial signature: the entry's PartialState
    and the requested refresh_mode reach upload_prepared_image.

    The library keeps the ``upload_prepared_image(prepared, refresh_mode=,
    state=)`` contract for pipe-partial, so the integration needs no behavior
    change -- this guards that the live path keeps threading both kwargs.
    """
    hass, entry, _, manager = _make_env(
        profile=_profile(sleep_mode="off"), last_seen=None
    )
    device = MagicMock()
    device.upload_prepared_image = AsyncMock()
    od = _device_ctx_factory(device=device)
    p1, p2, p3, p4, p5 = _patches(od)
    with p1, p2, p3, p4, p5:
        receipt = await _send(hass, entry)

    assert receipt.status == "delivered"
    device.upload_prepared_image.assert_awaited_once()
    call = device.upload_prepared_image.await_args
    # _send uses RefreshMode.FULL, so the service re-baselines partial_state to
    # a fresh PartialState and hands that same object to the library.
    assert call.kwargs["state"] is entry.runtime_data.partial_state
    assert call.kwargs["refresh_mode"] is RefreshMode.FULL


@pytest.mark.asyncio
async def test_live_send_waits_for_preheld_registry_lock(caplog):
    """A live send queued behind a pre-held per-MAC lock warns and waits."""
    caplog.set_level(logging.WARNING, logger="custom_components.opendisplay.ble_lock")
    hass, entry, _, _ = _make_env(profile=_profile(sleep_mode="off"), last_seen=None)
    device = MagicMock()
    device.upload_prepared_image = AsyncMock()
    order: list[str] = []
    release = asyncio.Event()

    async def _hold() -> None:
        async with ble_connection(ADDRESS, "external holder"):
            order.append("holder-enter")
            await release.wait()
            order.append("holder-exit")

    def _od_factory(**kwargs):
        class _Ctx:
            async def __aenter__(self):
                order.append("send-connect")
                return device

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    p1, p2, p3, p4, p5 = _patches(MagicMock(side_effect=_od_factory))
    with p1, p2, p3, p4, p5:
        holder = asyncio.create_task(_hold())
        while "holder-enter" not in order:
            await asyncio.sleep(0)
        send = asyncio.create_task(_send(hass, entry))
        # The send must block on the held lock before it ever connects.
        for _ in range(5):
            await asyncio.sleep(0)
        assert "send-connect" not in order
        release.set()
        receipt, _ = await asyncio.gather(send, holder)

    assert order == ["holder-enter", "holder-exit", "send-connect"]
    assert receipt.status == "delivered"
    warnings = [
        r for r in caplog.records
        if r.name == "custom_components.opendisplay.ble_lock"
        and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
