"""Tests for the per-entry deep-sleep upload queue.

Verifies:
- DeepSleepQueuedUpload expiry logic
- _async_send_image queues upload when device is not connectable
- Queued upload is flushed when the coordinator receives an advertisement
  and the device becomes connectable
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.opendisplay.deep_sleep import DeepSleepQueuedUpload
from custom_components.opendisplay.const import (
    DEFAULT_DEEP_SLEEP_EXPIRY_SECONDS,
)


# ---------------------------------------------------------------------------
# DeepSleepQueuedUpload unit tests
# ---------------------------------------------------------------------------


def _make_queued(*, seconds_old: float = 0, expiry_seconds: int = DEFAULT_DEEP_SLEEP_EXPIRY_SECONDS) -> DeepSleepQueuedUpload:
    return DeepSleepQueuedUpload(
        action=AsyncMock(),
        jpeg_bytes=b"",
        queued_at=datetime.now() - timedelta(seconds=seconds_old),
        expiry=timedelta(seconds=expiry_seconds),
    )


def test_queued_upload_not_expired_when_fresh() -> None:
    """A freshly queued upload is not expired."""
    q = _make_queued(seconds_old=0)
    assert not q.is_expired


def test_queued_upload_not_expired_just_before_expiry() -> None:
    """Upload is not expired just before its expiry window closes."""
    q = _make_queued(seconds_old=DEFAULT_DEEP_SLEEP_EXPIRY_SECONDS - 1)
    assert not q.is_expired


def test_queued_upload_expired_after_default_window() -> None:
    """Upload is expired after the default expiry window."""
    q = _make_queued(seconds_old=DEFAULT_DEEP_SLEEP_EXPIRY_SECONDS + 1)
    assert q.is_expired


def test_queued_upload_expired_with_custom_expiry() -> None:
    """Upload expiry respects a custom expiry timedelta."""
    q = _make_queued(seconds_old=3700, expiry_seconds=3600)
    assert q.is_expired


def test_queued_upload_not_expired_with_custom_expiry() -> None:
    """Upload not expired when within custom expiry window."""
    q = _make_queued(seconds_old=3500, expiry_seconds=3600)
    assert not q.is_expired


# ---------------------------------------------------------------------------
# _async_send_image queuing behaviour
# ---------------------------------------------------------------------------


def _make_entry(address: str = "AA:BB:CC:DD:EE:FF", deep_sleep_time_seconds: int = 3600) -> MagicMock:
    """Build a minimal mock config entry."""
    power = SimpleNamespace(deep_sleep_time_seconds=deep_sleep_time_seconds)
    device_config = SimpleNamespace(power=power)
    runtime_data = SimpleNamespace(
        deep_sleep_upload=None,
        deep_sleep_expiry_handle=None,
        device_config=device_config,
    )
    entry = MagicMock()
    entry.unique_id = address
    entry.runtime_data = runtime_data
    return entry


@pytest.mark.asyncio
async def test_send_image_queues_when_device_not_connectable() -> None:
    """Image upload is queued when the BLE device is not currently connectable."""
    hass = MagicMock()
    entry = _make_entry()

    img = MagicMock()

    from opendisplay import DitherMode, RefreshMode
    from custom_components.opendisplay.services import _async_send_image

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=None,  # device not connectable
    ):
        await _async_send_image(
            hass, entry, img, dither_mode=DitherMode.BURKES, refresh_mode=RefreshMode.FULL
        )

    # Upload should have been queued, not sent
    assert entry.runtime_data.deep_sleep_upload is not None
    assert not entry.runtime_data.deep_sleep_upload.is_expired


@pytest.mark.asyncio
async def test_send_image_uploads_immediately_when_connectable() -> None:
    """Image upload proceeds immediately when the BLE device is connectable."""
    hass = MagicMock()
    entry = _make_entry()

    img = MagicMock()

    from opendisplay import DitherMode, RefreshMode
    from custom_components.opendisplay.services import _async_send_image

    ble_device = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=b"jpeg")

    with (
        patch(
            "custom_components.opendisplay.services.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch(
            "custom_components.opendisplay.services._async_connect_and_run",
            new_callable=AsyncMock,
        ) as mock_run,
        patch(
            "custom_components.opendisplay.services._pil_to_jpeg",
            return_value=b"jpeg",
        ),
        patch("custom_components.opendisplay.services.async_dispatcher_send"),
    ):
        await _async_send_image(
            hass, entry, img, dither_mode=DitherMode.BURKES, refresh_mode=RefreshMode.FULL
        )

    # Upload should NOT have been queued
    assert entry.runtime_data.deep_sleep_upload is None
    # _async_connect_and_run should have been called
    mock_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_image_queued_upload_replaces_previous() -> None:
    """A new image upload replaces any previously queued upload."""
    hass = MagicMock()
    entry = _make_entry()
    first_upload = DeepSleepQueuedUpload(
        action=AsyncMock(),
        jpeg_bytes=b"",
        queued_at=datetime.now(),
        expiry=timedelta(seconds=3600),
    )
    entry.runtime_data.deep_sleep_upload = first_upload

    img = MagicMock()
    from opendisplay import DitherMode, RefreshMode
    from custom_components.opendisplay.services import _async_send_image

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=None,
    ):
        await _async_send_image(
            hass, entry, img, dither_mode=DitherMode.BURKES, refresh_mode=RefreshMode.FULL
        )

    new_upload = entry.runtime_data.deep_sleep_upload
    assert new_upload is not None
    assert new_upload is not first_upload


# ---------------------------------------------------------------------------
# Deep-sleep expiry derived from device config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expiry_derived_from_device_deep_sleep_time() -> None:
    """Expiry is computed as deep_sleep_time_seconds * 1.1 from device config."""
    hass = MagicMock()
    entry = _make_entry(deep_sleep_time_seconds=3600)

    img = MagicMock()
    from opendisplay import DitherMode, RefreshMode
    from custom_components.opendisplay.services import _async_send_image

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=None,
    ):
        await _async_send_image(
            hass, entry, img, dither_mode=DitherMode.BURKES, refresh_mode=RefreshMode.FULL
        )

    queued = entry.runtime_data.deep_sleep_upload
    assert queued is not None
    # 3600 * 1.1 = 3960 seconds
    assert queued.expiry == timedelta(seconds=3960)


@pytest.mark.asyncio
async def test_send_image_uploads_immediately_when_deep_sleep_is_unsupported() -> None:
    """Image upload is not queued when deep sleep is not configured."""
    hass = MagicMock()
    entry = _make_entry(deep_sleep_time_seconds=0)

    img = MagicMock()
    from opendisplay import DitherMode, RefreshMode
    from custom_components.opendisplay.services import _async_send_image

    hass.async_add_executor_job = AsyncMock(return_value=b"jpeg")

    with (
        patch(
            "custom_components.opendisplay.services.async_ble_device_from_address",
            return_value=None,
        ),
        patch(
            "custom_components.opendisplay.services._async_connect_and_run",
            new_callable=AsyncMock,
        ) as mock_run,
        patch(
            "custom_components.opendisplay.services._pil_to_jpeg",
            return_value=b"jpeg",
        ),
        patch("custom_components.opendisplay.services.async_dispatcher_send"),
    ):
        await _async_send_image(
            hass, entry, img, dither_mode=DitherMode.BURKES, refresh_mode=RefreshMode.FULL
        )

    assert entry.runtime_data.deep_sleep_upload is None
    mock_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_expiry_callback_purges_queued_upload_without_advertisement() -> None:
    """Queued upload is proactively removed when expiry timer callback runs."""
    hass = MagicMock()
    entry = _make_entry(deep_sleep_time_seconds=10)
    img = MagicMock()

    from opendisplay import DitherMode, RefreshMode
    from custom_components.opendisplay.services import _async_send_image

    fake_handle = MagicMock()
    callback_holder: dict[str, object] = {}

    def _capture_call_later(_seconds: int, callback):
        callback_holder["cb"] = callback
        return fake_handle

    hass.loop = MagicMock()
    hass.loop.call_later = MagicMock(side_effect=_capture_call_later)

    with patch(
        "custom_components.opendisplay.services.async_ble_device_from_address",
        return_value=None,
    ):
        await _async_send_image(
            hass, entry, img, dither_mode=DitherMode.BURKES, refresh_mode=RefreshMode.FULL
        )

    assert entry.runtime_data.deep_sleep_upload is not None
    assert entry.runtime_data.deep_sleep_expiry_handle is fake_handle

    callback_holder["cb"]()

    assert entry.runtime_data.deep_sleep_upload is None
    assert entry.runtime_data.deep_sleep_expiry_handle is None
