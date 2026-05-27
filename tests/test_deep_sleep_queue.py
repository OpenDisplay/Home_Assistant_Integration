from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.opendisplay import services as services_module
from custom_components.opendisplay.const import SIGNAL_TAG_CHECKIN
from custom_components.opendisplay.coordinator import Hub
from custom_components.opendisplay.upload import DeepSleepUploadQueue


@pytest.mark.asyncio
async def test_deep_sleep_queue_replaces_existing_image() -> None:
    """Queue keeps only the latest image for a sleeping tag."""
    queue = DeepSleepUploadQueue()

    async def upload_a():
        return None

    async def upload_b():
        return None

    await queue.queue_upload("aa:bb", upload_a, "first")
    await queue.queue_upload("AA:BB", upload_b, "second")

    queued = await queue.pop_upload("aa:bb")
    assert queued is not None
    assert queued.upload_func is upload_b
    assert queued.args == ("second",)


@pytest.mark.asyncio
async def test_deep_sleep_queue_default_expires_after_4_hours() -> None:
    """Queued image is dropped after expiration."""
    queue = DeepSleepUploadQueue()

    async def upload():
        return None

    await queue.queue_upload("aa:bb", upload, "payload")
    queue._pending_by_tag["AA:BB"].queued_at = (
        datetime.now() - timedelta(hours=4, minutes=1)
    )

    queued = await queue.pop_upload("aa:bb")
    assert queued is None


@pytest.mark.asyncio
async def test_deep_sleep_queue_uses_configured_expiry() -> None:
    """Queued image expiration follows configured queue timeout."""
    queue = DeepSleepUploadQueue(expiry=timedelta(hours=1))

    async def upload():
        return None

    await queue.queue_upload("aa:bb", upload, "payload")
    queue._pending_by_tag["AA:BB"].queued_at = datetime.now() - timedelta(minutes=61)

    queued = await queue.pop_upload("aa:bb")
    assert queued is None


@pytest.mark.asyncio
async def test_deep_sleep_queue_flushes_when_tag_checks_in(hass) -> None:
    """Queued upload is flushed when a sleeping tag wakes and checks in."""
    deep_sleep_upload_queue = DeepSleepUploadQueue()
    hub_upload_queue = SimpleNamespace(add_to_queue=AsyncMock())

    async def upload():
        return None

    await deep_sleep_upload_queue.queue_upload("aa:bb", upload, "payload")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            services_module,
            "create_upload_queues",
            lambda: (SimpleNamespace(), hub_upload_queue, deep_sleep_upload_queue),
        )
        mp.setattr(
            services_module,
            "get_hub_from_hass",
            lambda _hass: SimpleNamespace(),
        )
        await services_module.async_setup_services(hass)
        async_dispatcher_send(hass, SIGNAL_TAG_CHECKIN, "AA:BB")
        await hass.async_block_till_done()

        hub_upload_queue.add_to_queue.assert_awaited_once_with(upload, "payload")


def test_hub_should_queue_image_upload_for_sleeping_deep_sleep_tag() -> None:
    """Deep-sleeping tag should use pending upload queue."""
    now = datetime.now(timezone.utc).timestamp()
    hub = Hub.__new__(Hub)
    hub._data = {
        "AA:BB": {
            "modecfgjson": {"deepsleep": 1, "maxsleep": 60},
            "next_checkin": now + 60,
        }
    }

    assert hub.is_tag_in_deep_sleep("aa:bb")
    assert hub.is_tag_currently_sleeping("aa:bb")
    assert hub.should_queue_image_upload("aa:bb")


def test_hub_should_not_queue_when_tag_not_sleeping() -> None:
    """Awake tag should upload immediately."""
    now = datetime.now(timezone.utc).timestamp()
    hub = Hub.__new__(Hub)
    hub._data = {
        "AA:BB": {
            "modecfgjson": {"deepsleep": 1, "maxsleep": 60},
            "next_checkin": now - 5,
        }
    }

    assert hub.is_tag_in_deep_sleep("aa:bb")
    assert not hub.is_tag_currently_sleeping("aa:bb")
    assert not hub.should_queue_image_upload("aa:bb")
