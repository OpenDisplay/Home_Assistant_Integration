"""Unit tests for DeliveryManager with mocked hass/coordinator/device.

These avoid the full Home Assistant test harness: the manager's HA touchpoints
(``async_call_later``, ``async_dispatcher_send``, ``async_ble_device_from_address``
and ``OpenDisplayDevice``) are patched in the delivery module namespace.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from opendisplay import (
    AuthenticationFailedError,
    BLEConnectionError,
    PartialState,
    RefreshMode,
)
import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.opendisplay import delivery as delivery_mod
from custom_components.opendisplay.const import (
    CONF_BLOCKS_PER_ACK,
    CONF_MAX_QUEUE_SIZE,
    DEFAULT_BLOCKS_PER_ACK,
    DEFAULT_MAX_QUEUE_SIZE,
    EVENT_CONTENT_DELIVERED,
    EVENT_CONTENT_EXPIRED,
)
from custom_components.opendisplay.delivery import DeliveryManager
from custom_components.opendisplay.sleep import SleepProfile
from custom_components.opendisplay.storage import StoredPendingUpload

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


def _make_env(profile=None, entry_data=None, last_seen=None, options=None):
    """Return (hass, entry, coordinator) with a fake runtime for the manager."""
    profile = profile or _profile()
    coordinator = MagicMock()
    coordinator.data = SimpleNamespace(last_seen=last_seen)
    coordinator.async_subscribe_device_seen = MagicMock(return_value=MagicMock())
    runtime = SimpleNamespace(
        coordinator=coordinator,
        sleep_profile=profile,
        device_config=MagicMock(),
        ble_lock=asyncio.Lock(),
        config_resync_pending=False,
        firmware=None,
        is_flex=False,
    )
    entry = MagicMock()
    entry.unique_id = ADDRESS
    entry.runtime_data = runtime
    entry.data = entry_data if entry_data is not None else {}
    entry.options = options if options is not None else {}
    entry.async_start_reauth = MagicMock()
    hass = MagicMock()
    return hass, entry, coordinator


def _submit(mgr, device_id="dev1"):
    return mgr.submit_upload(
        prepared=(b"img", None, object()),
        refresh_mode=RefreshMode.FULL,
        partial_state=MagicMock(),
        use_measured_palettes=False,
        preview_jpeg=b"jpeg",
        device_id=device_id,
    )


def _fake_device_ctx(device):
    """Return a factory producing an async-context-manager wrapping device."""

    class _Ctx:
        async def __aenter__(self):
            return device

        async def __aexit__(self, *exc):
            return False

    return lambda **kwargs: _Ctx()


def _raising_device_ctx(exc):
    class _Ctx:
        async def __aenter__(self):
            raise exc

        async def __aexit__(self, *exc_info):
            return False

    return lambda **kwargs: _Ctx()


def test_submit_upload_queues_and_reports():
    hass, entry, _ = _make_env()
    with (
        patch.object(delivery_mod, "async_call_later", return_value=MagicMock()) as later,
        patch.object(delivery_mod, "async_dispatcher_send") as dispatch,
    ):
        mgr = DeliveryManager(hass, entry)
        receipt = _submit(mgr)

    assert receipt.status == "queued"
    assert receipt.expires_at is not None
    snap = mgr.state
    assert snap.pending is True
    assert snap.queued_at is not None
    assert snap.attempts == 0
    later.assert_called_once()  # deadline armed
    # Both the image preview and the pending-state signals were dispatched.
    assert dispatch.call_count == 2


def test_latest_wins_cancels_previous_deadline():
    hass, entry, _ = _make_env()
    cancels = [MagicMock(), MagicMock()]
    with (
        patch.object(delivery_mod, "async_call_later", side_effect=cancels),
        patch.object(delivery_mod, "async_dispatcher_send"),
    ):
        mgr = DeliveryManager(hass, entry)
        _submit(mgr, device_id="a")
        _submit(mgr, device_id="b")

    cancels[0].assert_called_once()  # the first deadline timer was cancelled
    assert mgr.state.pending is True


def test_expiry_clears_slot_and_fires_event():
    hass, entry, _ = _make_env()
    captured = {}

    def _fake_later(_hass, _delay, callback):
        captured["cb"] = callback
        return MagicMock()

    with (
        patch.object(delivery_mod, "async_call_later", side_effect=_fake_later),
        patch.object(delivery_mod, "async_dispatcher_send"),
    ):
        mgr = DeliveryManager(hass, entry)
        _submit(mgr, device_id="dev1")
        captured["cb"](None)  # simulate the deadline firing

    assert mgr.state.pending is False
    assert mgr.state.last_error == "expired"
    hass.bus.async_fire.assert_called_once()
    event, payload = hass.bus.async_fire.call_args[0]
    assert event == EVENT_CONTENT_EXPIRED
    assert payload["device_id"] == "dev1"
    assert "queued_at" in payload


def test_request_config_resync_sets_flags():
    hass, entry, _ = _make_env()
    mgr = DeliveryManager(hass, entry)
    assert mgr._has_pending_work() is False
    mgr.request_config_resync()
    assert entry.runtime_data.config_resync_pending is True
    assert mgr._has_pending_work() is True


def test_restore_pending_upload_rearms_existing_expiry():
    hass, entry, _ = _make_env()
    expires_at = 1234.0
    upload = StoredPendingUpload(
        prepared=(b"img", None, MagicMock()),
        refresh_mode=RefreshMode.FAST,
        partial_state=PartialState(),
        use_measured_palettes=True,
        preview_jpeg=b"jpeg",
        device_id="dev1",
        queued_at=1000.0,
        expires_at=expires_at,
        attempts=2,
    )

    with (
        patch.object(delivery_mod.time, "time", return_value=1200.0),
        patch.object(delivery_mod, "async_call_later", return_value=MagicMock()) as later,
        patch.object(delivery_mod, "async_dispatcher_send") as dispatch,
    ):
        mgr = DeliveryManager(hass, entry)
        mgr.restore_pending_upload(upload)

    assert mgr.state.pending is True
    assert mgr.state.queued_at == 1000.0
    assert mgr.state.expires_at == expires_at
    assert mgr.state.attempts == 2
    later.assert_called_once()
    assert later.call_args.args[1] == 34.0
    dispatch.assert_called_once()


def test_notify_device_seen_starts_one_delivery():
    hass, entry, _ = _make_env()

    def _capture(_hass, coro, _name):
        coro.close()  # don't actually run the drain
        return MagicMock()

    entry.async_create_background_task = MagicMock(side_effect=_capture)
    with (
        patch.object(delivery_mod, "async_call_later", return_value=MagicMock()),
        patch.object(delivery_mod, "async_dispatcher_send"),
    ):
        mgr = DeliveryManager(hass, entry)
        mgr.notify_device_seen()  # nothing queued -> no delivery
        entry.async_create_background_task.assert_not_called()

        _submit(mgr)
        mgr.notify_device_seen()  # work queued -> one delivery
        entry.async_create_background_task.assert_called_once()

        mgr.notify_device_seen()  # already delivering -> still one
        entry.async_create_background_task.assert_called_once()


@pytest.mark.asyncio
async def test_drain_delivers_upload():
    hass, entry, _ = _make_env()
    device = MagicMock()
    device.upload_prepared_image = AsyncMock()
    with (
        patch.object(delivery_mod, "async_call_later", return_value=MagicMock()),
        patch.object(delivery_mod, "async_dispatcher_send"),
        patch.object(delivery_mod, "async_ble_device_from_address", return_value=MagicMock()),
        patch.object(delivery_mod, "OpenDisplayDevice", side_effect=_fake_device_ctx(device)),
    ):
        mgr = DeliveryManager(hass, entry)
        _submit(mgr, device_id="dev1")
        await mgr._deliver()

    device.upload_prepared_image.assert_awaited_once()
    assert mgr.state.pending is False
    fired = [call.args[0] for call in hass.bus.async_fire.call_args_list]
    assert EVENT_CONTENT_DELIVERED in fired


@pytest.mark.asyncio
async def test_drain_ble_failure_keeps_slot_and_counts_attempt():
    hass, entry, _ = _make_env()
    with (
        patch.object(delivery_mod, "async_call_later", return_value=MagicMock()),
        patch.object(delivery_mod, "async_dispatcher_send"),
        patch.object(delivery_mod, "async_ble_device_from_address", return_value=MagicMock()),
        patch.object(
            delivery_mod,
            "OpenDisplayDevice",
            side_effect=_raising_device_ctx(BLEConnectionError("boom")),
        ),
    ):
        mgr = DeliveryManager(hass, entry)
        _submit(mgr)
        await mgr._deliver()

    assert mgr.state.pending is True
    assert mgr.state.attempts == 1


@pytest.mark.asyncio
async def test_drain_deadline_timeout_raises_and_counts_attempt():
    """Exceeding DELIVERY_DEADLINE_S raises loudly but keeps the slot queued."""
    hass, entry, _ = _make_env()
    with (
        patch.object(delivery_mod, "async_call_later", return_value=MagicMock()),
        patch.object(delivery_mod, "async_dispatcher_send"),
        patch.object(delivery_mod, "async_ble_device_from_address", return_value=MagicMock()),
        patch.object(
            delivery_mod,
            "OpenDisplayDevice",
            side_effect=_raising_device_ctx(TimeoutError()),
        ),
    ):
        mgr = DeliveryManager(hass, entry)
        _submit(mgr)
        with pytest.raises(HomeAssistantError):
            await mgr._deliver()

    # The slot is retained for the next wake and the failed attempt is recorded.
    assert mgr.state.pending is True
    assert mgr.state.attempts == 1
    assert "deadline exceeded" in (mgr.state.last_error or "")
    # Background-task bookkeeping is still cleaned up despite the raise.
    assert mgr._delivering is False
    assert mgr._delivery_task is None


@pytest.mark.asyncio
async def test_drain_gives_up_after_max_attempts():
    """After MAX_DELIVERY_ATTEMPTS failures the slot is dropped with an expired event."""
    hass, entry, _ = _make_env()
    deadline_cancel = MagicMock()
    with (
        patch.object(delivery_mod, "async_call_later", return_value=deadline_cancel),
        patch.object(delivery_mod, "async_dispatcher_send"),
        patch.object(delivery_mod, "async_ble_device_from_address", return_value=MagicMock()),
        patch.object(
            delivery_mod,
            "OpenDisplayDevice",
            side_effect=_raising_device_ctx(BLEConnectionError("boom")),
        ),
    ):
        mgr = DeliveryManager(hass, entry)
        _submit(mgr)
        for attempt in range(1, delivery_mod.MAX_DELIVERY_ATTEMPTS + 1):
            await mgr._deliver()
            if attempt < delivery_mod.MAX_DELIVERY_ATTEMPTS:
                # Still queued and retriable until the cap is reached.
                assert mgr.state.pending is True
                assert mgr.state.attempts == attempt

    # On the capped attempt the slot is dropped and the expiry timer cancelled.
    assert mgr.state.pending is False
    assert mgr.state.last_error == "failed"
    deadline_cancel.assert_called_once()

    # A content_expired event fired once, carrying attempts == the cap.
    expired = [
        c.args[1]
        for c in hass.bus.async_fire.call_args_list
        if c.args[0] == EVENT_CONTENT_EXPIRED
    ]
    assert len(expired) == 1
    assert expired[0]["attempts"] == delivery_mod.MAX_DELIVERY_ATTEMPTS


@pytest.mark.asyncio
async def test_drain_auth_failure_pauses_and_starts_reauth():
    hass, entry, _ = _make_env()
    with (
        patch.object(delivery_mod, "async_call_later", return_value=MagicMock()),
        patch.object(delivery_mod, "async_dispatcher_send"),
        patch.object(delivery_mod, "async_ble_device_from_address", return_value=MagicMock()),
        patch.object(
            delivery_mod,
            "OpenDisplayDevice",
            side_effect=_raising_device_ctx(AuthenticationFailedError("bad key")),
        ),
    ):
        mgr = DeliveryManager(hass, entry)
        _submit(mgr)
        await mgr._deliver()

    assert mgr._pending_upload is not None
    assert mgr._pending_upload.paused is True
    assert mgr.state.last_error == "auth"
    entry.async_start_reauth.assert_called_once()
    # A paused slot is not retried on the next wake.
    assert mgr._has_pending_work() is False


@pytest.mark.asyncio
async def test_drain_config_resync_updates_runtime_and_cache():
    hass, entry, _ = _make_env()
    device = MagicMock()
    device.read_firmware_version = AsyncMock(
        return_value={"major": 1, "minor": 2, "sha": "abc"}
    )
    device.is_flex = False
    device.landing_url = MagicMock(return_value="http://landing")
    device.config = MagicMock()
    with (
        patch.object(delivery_mod, "async_call_later", return_value=MagicMock()),
        patch.object(delivery_mod, "async_dispatcher_send"),
        patch.object(delivery_mod, "async_ble_device_from_address", return_value=MagicMock()),
        patch.object(delivery_mod, "OpenDisplayDevice", side_effect=_fake_device_ctx(device)),
        patch("custom_components.opendisplay._write_cache") as write_cache,
    ):
        mgr = DeliveryManager(hass, entry)
        mgr.request_config_resync()
        await mgr._deliver()

    assert entry.runtime_data.config_resync_pending is False
    assert mgr._pending_config_resync is False
    assert entry.runtime_data.firmware == {"major": 1, "minor": 2, "sha": "abc"}
    write_cache.assert_called_once()


@pytest.mark.asyncio
async def test_shutdown_unsubscribes_and_cancels_deadline():
    hass, entry, coordinator = _make_env()
    unsub = MagicMock()
    coordinator.async_subscribe_device_seen = MagicMock(return_value=unsub)
    deadline_cancel = MagicMock()
    with (
        patch.object(delivery_mod, "async_call_later", return_value=deadline_cancel),
        patch.object(delivery_mod, "async_dispatcher_send"),
    ):
        mgr = DeliveryManager(hass, entry)
        mgr.async_start()
        _submit(mgr)
        await mgr.async_shutdown()

    unsub.assert_called_once()
    deadline_cancel.assert_called_once()


@pytest.mark.asyncio
async def test_drain_threads_pipe_kwargs_default():
    """No options set: library sliding-window defaults reach the constructor."""
    hass, entry, _ = _make_env()
    device = MagicMock()
    device.upload_prepared_image = AsyncMock()
    with (
        patch.object(delivery_mod, "async_call_later", return_value=MagicMock()),
        patch.object(delivery_mod, "async_dispatcher_send"),
        patch.object(delivery_mod, "async_ble_device_from_address", return_value=MagicMock()),
        patch.object(
            delivery_mod, "OpenDisplayDevice", side_effect=_fake_device_ctx(device)
        ) as od,
    ):
        mgr = DeliveryManager(hass, entry)
        _submit(mgr)
        await mgr._deliver()

    device.upload_prepared_image.assert_awaited_once()
    kwargs = od.call_args.kwargs
    assert kwargs["blocks_per_ack"] == DEFAULT_BLOCKS_PER_ACK
    assert kwargs["max_queue_size"] == DEFAULT_MAX_QUEUE_SIZE


@pytest.mark.asyncio
async def test_drain_threads_pipe_kwargs_custom():
    """Configured entry options reach the OpenDisplayDevice constructor."""
    hass, entry, _ = _make_env(
        options={CONF_BLOCKS_PER_ACK: 4, CONF_MAX_QUEUE_SIZE: 1}
    )
    device = MagicMock()
    device.upload_prepared_image = AsyncMock()
    with (
        patch.object(delivery_mod, "async_call_later", return_value=MagicMock()),
        patch.object(delivery_mod, "async_dispatcher_send"),
        patch.object(delivery_mod, "async_ble_device_from_address", return_value=MagicMock()),
        patch.object(
            delivery_mod, "OpenDisplayDevice", side_effect=_fake_device_ctx(device)
        ) as od,
    ):
        mgr = DeliveryManager(hass, entry)
        _submit(mgr)
        await mgr._deliver()

    device.upload_prepared_image.assert_awaited_once()
    kwargs = od.call_args.kwargs
    assert kwargs["blocks_per_ack"] == 4
    assert kwargs["max_queue_size"] == 1


@pytest.mark.asyncio
async def test_drain_passes_state_and_refresh_mode():
    """Wake/queued delivery pins the pipe-partial signature: the entry's
    PartialState and the queued refresh_mode reach upload_prepared_image.

    The library keeps the ``upload_prepared_image(prepared, refresh_mode=,
    state=)`` contract for pipe-partial, so the integration needs no behavior
    change -- this guards that the wake path keeps threading both kwargs.
    """
    hass, entry, _ = _make_env()
    device = MagicMock()
    device.upload_prepared_image = AsyncMock()
    sentinel_state = MagicMock()
    with (
        patch.object(delivery_mod, "async_call_later", return_value=MagicMock()),
        patch.object(delivery_mod, "async_dispatcher_send"),
        patch.object(
            delivery_mod, "async_ble_device_from_address", return_value=MagicMock()
        ),
        patch.object(
            delivery_mod, "OpenDisplayDevice", side_effect=_fake_device_ctx(device)
        ),
    ):
        mgr = DeliveryManager(hass, entry)
        mgr.submit_upload(
            prepared=(b"img", None, object()),
            refresh_mode=RefreshMode.PARTIAL,
            partial_state=sentinel_state,
            use_measured_palettes=False,
            preview_jpeg=b"jpeg",
            device_id="dev1",
        )
        await mgr._deliver()

    device.upload_prepared_image.assert_awaited_once()
    call = device.upload_prepared_image.await_args
    assert call.kwargs["state"] is sentinel_state
    assert call.kwargs["refresh_mode"] is RefreshMode.PARTIAL


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
