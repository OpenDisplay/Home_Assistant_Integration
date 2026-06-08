"""Tests for pending upload deep-sleep behavior."""

import asyncio

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.opendisplay.services import (
    PendingDisplayUpload,
    _async_queue_or_send_image,
    _async_try_pending_upload,
    _pending_upload_timeout_seconds,
    async_register_pending_upload_listener,
)
from custom_components.opendisplay.deep_sleep import availability_window_seconds


def _make_entry(*, available: bool = False, deep_sleep_seconds: int = 300):
    runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            available=available,
            expected_wakeup_timestamp=None,
            deep_sleep_availability_deadline_timestamp=None,
            deep_sleep_availability_window_seconds=availability_window_seconds(
                deep_sleep_seconds
            ),
            async_set_pending_upload=MagicMock(),
        ),
        pending_upload=None,
        pending_upload_task=None,
        pending_upload_expiry_unsub=None,
        device_config=SimpleNamespace(
            power=SimpleNamespace(deep_sleep_time_seconds=deep_sleep_seconds)
        ),
    )
    entry = MagicMock()
    entry.unique_id = "AA:BB:CC:DD:EE:FF"
    entry.options = {}
    entry.runtime_data = runtime_data
    return entry


def test_pending_upload_timeout_resets_even_when_old_deadline_is_near():
    entry = _make_entry(available=False, deep_sleep_seconds=120)
    entry.runtime_data.coordinator.deep_sleep_availability_deadline_timestamp = (
        dt_util.utcnow() + timedelta(seconds=30)
    )

    assert _pending_upload_timeout_seconds(entry) == availability_window_seconds(120)


@pytest.mark.asyncio
async def test_queue_or_send_stores_pending_without_retry_when_device_sleeping():
    hass = MagicMock()
    entry = _make_entry(available=False)
    cancel_expiry = MagicMock()
    pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="upload_image",
    )

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=None,
    ), patch(
        "custom_components.opendisplay.services._async_send_image_now",
        new_callable=AsyncMock,
    ) as mock_send_now, patch(
        "custom_components.opendisplay.services.async_call_later",
        return_value=cancel_expiry,
    ) as mock_call_later, patch(
        "custom_components.opendisplay.services.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        await _async_queue_or_send_image(hass, entry, pending)

    assert entry.runtime_data.pending_upload is pending
    assert pending.expires_at is not None
    assert entry.runtime_data.pending_upload_expiry_unsub is cancel_expiry
    mock_call_later.assert_called_once()
    mock_send_now.assert_not_awaited()
    mock_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_or_send_retries_then_stores_pending_when_deep_sleep_awake_fails():
    hass = MagicMock()
    entry = _make_entry(available=True)
    cancel_expiry = MagicMock()
    pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="upload_image",
    )

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=MagicMock(),
    ), patch(
        "custom_components.opendisplay.services._async_send_image_now",
        new_callable=AsyncMock,
        side_effect=HomeAssistantError("not ready"),
    ) as mock_send_now, patch(
        "custom_components.opendisplay.services.async_call_later",
        return_value=cancel_expiry,
    ), patch(
        "custom_components.opendisplay.services.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        await _async_queue_or_send_image(hass, entry, pending)

    assert entry.runtime_data.pending_upload is pending
    assert pending.expires_at is not None
    assert mock_send_now.await_count == 3
    assert mock_sleep.await_count == 2


@pytest.mark.asyncio
async def test_queue_or_send_replaces_existing_pending_and_resets_ttl():
    hass = MagicMock()
    entry = _make_entry(available=False)
    old_cancel_expiry = MagicMock()
    old_task = MagicMock()
    old_task.done.return_value = False
    old_pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="upload_image",
    )
    old_pending.expires_at = old_pending.created_at + timedelta(seconds=120)
    new_pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="drawcustom",
    )
    new_cancel_expiry = MagicMock()
    entry.runtime_data.pending_upload = old_pending
    entry.runtime_data.pending_upload_expiry_unsub = old_cancel_expiry
    entry.runtime_data.pending_upload_task = old_task

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=None,
    ), patch(
        "custom_components.opendisplay.services._async_send_image_now",
        new_callable=AsyncMock,
        side_effect=HomeAssistantError("not connectable"),
    ), patch(
        "custom_components.opendisplay.services.async_call_later",
        return_value=new_cancel_expiry,
    ), patch(
        "custom_components.opendisplay.services.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        await _async_queue_or_send_image(hass, entry, new_pending)

    assert entry.runtime_data.pending_upload is new_pending
    assert entry.runtime_data.pending_upload_expiry_unsub is new_cancel_expiry
    assert entry.runtime_data.pending_upload_task is None
    assert new_pending.expires_at is not None
    old_cancel_expiry.assert_called_once()
    old_task.cancel.assert_called_once()
    entry.runtime_data.coordinator.async_set_pending_upload.assert_called_with(True)


@pytest.mark.asyncio
async def test_immediate_upload_clears_existing_pending():
    hass = MagicMock()
    entry = _make_entry(available=True)
    old_cancel_expiry = MagicMock()
    old_pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="upload_image",
    )
    new_pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="drawcustom",
    )
    entry.runtime_data.pending_upload = old_pending
    entry.runtime_data.pending_upload_expiry_unsub = old_cancel_expiry

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=MagicMock(),
    ), patch(
        "custom_components.opendisplay.services._async_send_image_now",
        new_callable=AsyncMock,
    ) as mock_send_now:
        await _async_queue_or_send_image(hass, entry, new_pending)

    mock_send_now.assert_awaited_once_with(hass, entry, new_pending)
    assert entry.runtime_data.pending_upload is None
    assert entry.runtime_data.pending_upload_expiry_unsub is None
    old_cancel_expiry.assert_called_once()
    entry.runtime_data.coordinator.async_set_pending_upload.assert_called_with(False)


@pytest.mark.asyncio
async def test_queue_or_send_does_not_queue_non_deep_sleep_failure():
    hass = MagicMock()
    entry = _make_entry(available=True, deep_sleep_seconds=0)
    pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="upload_image",
    )

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=MagicMock(),
    ), patch(
        "custom_components.opendisplay.services._async_send_image_now",
        new_callable=AsyncMock,
        side_effect=HomeAssistantError("upload failed"),
    ), patch(
        "custom_components.opendisplay.services.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        with pytest.raises(HomeAssistantError):
            await _async_queue_or_send_image(hass, entry, pending)

    assert mock_sleep.await_count == 2
    assert entry.runtime_data.pending_upload is None
    assert entry.runtime_data.pending_upload_expiry_unsub is None


@pytest.mark.asyncio
async def test_immediate_upload_retries_then_succeeds():
    hass = MagicMock()
    entry = _make_entry(available=True, deep_sleep_seconds=0)
    pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="upload_image",
    )

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=MagicMock(),
    ), patch(
        "custom_components.opendisplay.services._async_send_image_now",
        new_callable=AsyncMock,
        side_effect=[HomeAssistantError("busy"), None],
    ) as mock_send_now, patch(
        "custom_components.opendisplay.services.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        await _async_queue_or_send_image(hass, entry, pending)

    assert mock_send_now.await_count == 2
    assert mock_sleep.await_count == 1
    assert entry.runtime_data.pending_upload is None


@pytest.mark.asyncio
async def test_try_pending_upload_drops_pending_after_retry_failure():
    hass = MagicMock()
    hass.async_create_task = lambda coro, name=None: asyncio.create_task(coro)
    entry = _make_entry(available=True)
    pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="upload_image",
    )
    entry.runtime_data.pending_upload = pending

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=MagicMock(),
    ), patch(
        "custom_components.opendisplay.services._async_send_image_now",
        new_callable=AsyncMock,
        side_effect=HomeAssistantError("upload failed"),
    ), patch(
        "custom_components.opendisplay.services.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        await _async_try_pending_upload(hass, entry)
        if entry.runtime_data.pending_upload_task is not None:
            await entry.runtime_data.pending_upload_task

    assert entry.runtime_data.pending_upload is None
    entry.runtime_data.coordinator.async_set_pending_upload.assert_called_with(False)
    assert mock_sleep.await_count == 3


@pytest.mark.asyncio
async def test_try_pending_upload_clears_pending_on_success():
    hass = MagicMock()
    hass.async_create_task = lambda coro, name=None: asyncio.create_task(coro)
    entry = _make_entry(available=True)
    cancel_expiry = MagicMock()
    pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="upload_image",
    )
    pending.expires_at = pending.created_at + timedelta(seconds=360)
    entry.runtime_data.pending_upload = pending
    entry.runtime_data.pending_upload_expiry_unsub = cancel_expiry

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=MagicMock(),
    ), patch(
        "custom_components.opendisplay.services._async_send_image_now",
        new_callable=AsyncMock,
    ) as mock_send_now, patch(
        "custom_components.opendisplay.services.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        await _async_try_pending_upload(hass, entry)
        if entry.runtime_data.pending_upload_task is not None:
            await entry.runtime_data.pending_upload_task

    mock_send_now.assert_awaited_once_with(hass, entry, pending)
    assert mock_sleep.await_count == 1
    assert entry.runtime_data.pending_upload is None
    assert entry.runtime_data.pending_upload_expiry_unsub is None
    cancel_expiry.assert_called_once()


@pytest.mark.asyncio
async def test_try_pending_upload_retries_then_succeeds():
    hass = MagicMock()
    hass.async_create_task = lambda coro, name=None: asyncio.create_task(coro)
    entry = _make_entry(available=True)
    pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="upload_image",
    )
    entry.runtime_data.pending_upload = pending

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=MagicMock(),
    ), patch(
        "custom_components.opendisplay.services._async_send_image_now",
        new_callable=AsyncMock,
        side_effect=[HomeAssistantError("busy"), None],
    ) as mock_send_now, patch(
        "custom_components.opendisplay.services.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        await _async_try_pending_upload(hass, entry)
        if entry.runtime_data.pending_upload_task is not None:
            await entry.runtime_data.pending_upload_task

    assert mock_send_now.await_count == 2
    assert mock_sleep.await_count == 2
    assert entry.runtime_data.pending_upload is None


@pytest.mark.asyncio
async def test_try_pending_upload_defers_when_not_connectable():
    hass = MagicMock()
    hass.async_create_task = lambda coro, name=None: asyncio.create_task(coro)
    entry = _make_entry(available=True)
    pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="upload_image",
    )
    entry.runtime_data.pending_upload = pending

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=None,
    ), patch(
        "custom_components.opendisplay.services._async_send_image_now",
        new_callable=AsyncMock,
    ) as mock_send:
        await _async_try_pending_upload(hass, entry)

    assert entry.runtime_data.pending_upload is pending
    assert entry.runtime_data.pending_upload_task is None
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_upload_expiry_drops_pending():
    hass = MagicMock()
    entry = _make_entry(available=False)
    captured_callback = None
    cancel_expiry = MagicMock()
    pending = PendingDisplayUpload(
        image=MagicMock(),
        dither_mode=MagicMock(),
        refresh_mode=MagicMock(),
        source="upload_image",
    )

    def _fake_call_later(_hass, _delay, callback):
        nonlocal captured_callback
        captured_callback = callback
        return cancel_expiry

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=None,
    ), patch(
        "custom_components.opendisplay.services.async_call_later",
        side_effect=_fake_call_later,
    ):
        await _async_queue_or_send_image(hass, entry, pending)

    assert captured_callback is not None
    captured_callback(pending.expires_at)

    assert entry.runtime_data.pending_upload is None
    assert entry.runtime_data.pending_upload_expiry_unsub is None
    entry.runtime_data.coordinator.async_set_pending_upload.assert_called_with(False)


def test_pending_upload_listener_skips_scheduling_without_pending() -> None:
    hass = MagicMock()
    entry = _make_entry(available=True)

    captured_callback = None

    def _fake_dispatcher_connect(_hass, _signal, callback):
        nonlocal captured_callback
        captured_callback = callback
        return lambda: None

    with patch(
        "custom_components.opendisplay.services.async_dispatcher_connect",
        side_effect=_fake_dispatcher_connect,
    ):
        async_register_pending_upload_listener(hass, entry)

    assert captured_callback is not None
    captured_callback()
    hass.async_create_task.assert_not_called()
