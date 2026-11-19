"""Test DeliveryManager: the queue that holds work for a sleeping tag.

Only the connection touchpoints are patched, in the transport module namespace
that owns the connect/fallback flow. The config entry, its runtime_data, the
event bus and the expiry timers are all real, so a deadline is fired by moving
Home Assistant's clock rather than by hand-calling a captured callback.
"""

import asyncio
from datetime import timedelta
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from opendisplay import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    OpenDisplayError,
    ProtocolError,
    RefreshMode,
)
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.opendisplay import delivery as delivery_mod
from custom_components.opendisplay.binary_sensor import OpenDisplayUpdatePendingSensor
from custom_components.opendisplay.ble_lock import async_get_ble_lock, ble_connection
from custom_components.opendisplay.const import (
    CONF_BLOCKS_PER_ACK,
    CONF_ENCRYPTION_KEY,
    CONF_HOST,
    CONF_MAX_QUEUE_SIZE,
    CONF_PORT,
    CONF_SLEEP_AFTER_DELIVERY,
    CONF_SLEEP_MODE,
    CONF_TLS,
    DEFAULT_BLOCKS_PER_ACK,
    DEFAULT_MAX_QUEUE_SIZE,
    EVENT_CONTENT_DELIVERED,
    EVENT_CONTENT_EXPIRED,
)
from custom_components.opendisplay.delivery import DeliveryManager

from . import (
    TEST_ADDRESS as ADDRESS,
    _async_ctx,
    connection_fails,
    connects_to,
    connects_via,
    make_sleepy_device_config,
    make_v1_service_info,
)
from .bluetooth import inject_bluetooth_service_info

# Every test here is about a tag that sleeps: a device that is always awake
# delivers immediately and never queues anything.
pytestmark = pytest.mark.parametrize(
    "device_config", [make_sleepy_device_config()], ids=[""]
)


@pytest.fixture
def platforms() -> list[Platform]:
    """No platforms; these tests drive the manager directly."""
    return []


@pytest.fixture
async def entry(mock_config_entry: MockConfigEntry, setup_entry) -> MockConfigEntry:
    """Return a loaded config entry for a deep-sleeping device."""
    await setup_entry()
    return mock_config_entry


@pytest.fixture
def manager(hass: HomeAssistant, entry: MockConfigEntry) -> DeliveryManager:
    """Return a manager bound to the real entry, as async_setup_entry builds one."""
    return DeliveryManager(hass, entry)


def _submit(mgr: DeliveryManager, device_id: str = "dev1", **overrides):
    """Queue an image the way the upload service does."""
    kwargs = {
        "prepared": (b"img", None, object()),
        "refresh_mode": RefreshMode.FULL,
        "partial_state": MagicMock(),
        "use_measured_palettes": False,
        "preview_jpeg": b"jpeg",
        "device_id": device_id,
    }
    kwargs.update(overrides)
    return mgr.submit_upload(**kwargs)


def _reauth_flows(hass: HomeAssistant) -> list:
    """Return the reauth flows currently in progress for this integration."""
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler("opendisplay")
        if flow["context"]["source"] == "reauth"
    ]


def _uploading_device() -> MagicMock:
    """Return a device mock that accepts a prepared image."""
    device = MagicMock()
    device.upload_prepared_image = AsyncMock()
    device.deep_sleep = AsyncMock()
    return device


def _resyncing_device() -> MagicMock:
    """Return a device mock that also answers a config resync."""
    device = _uploading_device()
    device.read_firmware_version = AsyncMock(
        return_value={"major": 1, "minor": 2, "sha": "abc"}
    )
    device.is_flex = False
    device.landing_url = MagicMock(return_value="http://landing")
    device.config = MagicMock()
    return device


# --- queueing --------------------------------------------------------------


async def test_submit_upload_queues_and_reports(manager: DeliveryManager) -> None:
    """A submitted frame is held with a queued-at stamp and an expiry."""
    receipt = _submit(manager)

    assert receipt.status == "queued"
    assert manager.state.pending is True
    assert manager.state.queued_at is not None
    assert manager.state.expires_at is not None


async def test_latest_wins_replaces_the_queued_frame(manager: DeliveryManager) -> None:
    """A newer frame supersedes an older one instead of queueing behind it."""
    _submit(manager, device_id="first")
    first_queued_at = manager.state.queued_at

    _submit(manager, device_id="second")

    assert manager._pending_upload.device_id == "second"
    assert manager.state.queued_at >= first_queued_at


async def test_expiry_clears_the_slot_and_fires_an_event(
    hass: HomeAssistant, manager: DeliveryManager
) -> None:
    """Content that is never delivered is dropped once its deadline passes.

    The deadline is a real timer, so this advances Home Assistant's clock past
    the queue timeout rather than invoking the callback directly.
    """
    expired = async_capture_events(hass, EVENT_CONTENT_EXPIRED)
    _submit(manager, device_id="dev1")

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=25))
    await hass.async_block_till_done()

    assert manager.state.pending is False
    assert manager.state.last_error == "expired"
    assert len(expired) == 1
    assert expired[0].data["device_id"] == "dev1"
    assert "queued_at" in expired[0].data


async def test_request_config_resync_sets_flags(
    entry: MockConfigEntry, manager: DeliveryManager
) -> None:
    """A resync request counts as pending work even with no image queued."""
    assert manager._has_pending_work() is False

    manager.request_config_resync()

    assert entry.runtime_data.config_resync_pending is True
    assert manager._has_pending_work() is True


async def test_notify_device_seen_starts_one_delivery(
    hass: HomeAssistant, manager: DeliveryManager
) -> None:
    """A wake starts a drain only when there is work, and only one at a time."""
    device = _uploading_device()
    with connects_to(device):
        manager.notify_device_seen()
        await hass.async_block_till_done()
        device.upload_prepared_image.assert_not_awaited()

        _submit(manager)
        manager.notify_device_seen()
        manager.notify_device_seen()  # already delivering
        await hass.async_block_till_done()

    device.upload_prepared_image.assert_awaited_once()


# --- draining --------------------------------------------------------------


async def test_drain_delivers_the_queued_upload(
    hass: HomeAssistant, manager: DeliveryManager
) -> None:
    """A successful drain uploads the frame and announces it."""
    delivered = async_capture_events(hass, EVENT_CONTENT_DELIVERED)
    device = _uploading_device()
    with connects_to(device):
        _submit(manager, device_id="dev1")
        await manager._deliver()
    await hass.async_block_till_done()

    device.upload_prepared_image.assert_awaited_once()
    assert manager.state.pending is False
    assert len(delivered) == 1


async def test_drain_ble_failure_keeps_the_slot_and_counts_an_attempt(
    manager: DeliveryManager,
) -> None:
    """A failed drain leaves the content queued for the next wake."""
    with connection_fails(BLEConnectionError("boom")):
        _submit(manager)
        await manager._deliver()

    assert manager.state.pending is True
    assert manager.state.attempts == 1


async def test_drain_deadline_raises_but_keeps_the_slot(
    manager: DeliveryManager,
) -> None:
    """Exceeding the delivery deadline raises loudly but does not lose content."""
    with connection_fails(TimeoutError()):
        _submit(manager)
        with pytest.raises(HomeAssistantError):
            await manager._deliver()

    assert manager.state.pending is True
    assert manager.state.attempts == 1
    assert "deadline exceeded" in (manager.state.last_error or "")
    # Background-task bookkeeping is cleaned up despite the raise.
    assert manager._delivering is False
    assert manager._delivery_task is None


async def test_drain_gives_up_after_max_attempts(
    hass: HomeAssistant, manager: DeliveryManager
) -> None:
    """The slot is dropped once the retry cap is hit, rather than retrying forever."""
    expired = async_capture_events(hass, EVENT_CONTENT_EXPIRED)
    with connection_fails(BLEConnectionError("boom")):
        _submit(manager)
        for attempt in range(1, delivery_mod.MAX_DELIVERY_ATTEMPTS + 1):
            await manager._deliver()
            if attempt < delivery_mod.MAX_DELIVERY_ATTEMPTS:
                assert manager.state.pending is True
                assert manager.state.attempts == attempt
    await hass.async_block_till_done()

    assert manager.state.pending is False
    assert manager.state.last_error == "failed"
    assert len(expired) == 1
    assert expired[0].data["attempts"] == delivery_mod.MAX_DELIVERY_ATTEMPTS


@pytest.mark.parametrize(
    "auth_error",
    [
        AuthenticationFailedError("wrong key"),
        # Issue #91's real case: firmware status 0x03 (encryption not
        # configured) surfaces as AuthenticationRequiredError.
        AuthenticationRequiredError("device has no encryption configured"),
    ],
    ids=["failed", "required"],
)
async def test_drain_auth_failure_pauses_and_starts_reauth(
    hass: HomeAssistant, manager: DeliveryManager, auth_error: Exception
) -> None:
    """A rejected key pauses the manager rather than burning the retry budget."""
    with connection_fails(auth_error):
        _submit(manager)
        await manager._deliver()
    await hass.async_block_till_done()

    # The slot survives (only a reload drops it); what stops the retry is the
    # manager-wide pause, not a per-slot flag.
    assert manager._pending_upload is not None
    assert manager._auth_paused is True
    assert manager.state.auth_paused is True
    assert manager.state.last_error == "auth"
    assert manager._has_pending_work() is False
    assert _reauth_flows(hass)


@pytest.mark.parametrize(
    "auth_error",
    [
        AuthenticationFailedError("wrong key"),
        AuthenticationRequiredError("device has no encryption configured"),
    ],
    ids=["failed", "required"],
)
async def test_auth_failure_stops_a_resync_only_retry_loop(
    hass: HomeAssistant, manager: DeliveryManager, auth_error: Exception
) -> None:
    """Regression for issue #91.

    With only a config resync queued, the pause used to be applied to the
    upload slot alone, which was empty. _has_pending_work() stayed true and the
    next advertisement started another identical, doomed session: roughly 20 a
    minute, indefinitely, draining the tag's battery.
    """
    device = _uploading_device()
    with connection_fails(auth_error):
        manager.request_config_resync()
        assert manager._has_pending_work() is True
        await manager._deliver()

    # The work is still queued, but it is no longer deliverable.
    assert manager._pending_config_resync is True
    assert manager._has_pending_work() is False

    # So the next advertisement starts no second session.
    with connects_to(device):
        manager.notify_device_seen()
        await hass.async_block_till_done()
    device.upload_prepared_image.assert_not_awaited()
    assert manager._delivering is False


async def test_new_content_does_not_bypass_the_auth_pause(
    manager: DeliveryManager,
) -> None:
    """Queuing new content must not re-arm the storm while the pause stands.

    submit_upload is routinely automation-driven, so clearing the pause there
    would let a frame-changing automation restart delivery on every wake.
    """
    with connection_fails(AuthenticationFailedError("bad key")):
        manager.request_config_resync()
        await manager._deliver()

        _submit(manager, device_id="dev2")

    assert manager._pending_upload is not None
    assert manager._has_pending_work() is False
    # auth_paused is authoritative; last_error is best effort, because an
    # expiring slot can overwrite it with "expired".
    assert manager.state.auth_paused is True
    assert manager.state.last_error == "auth"


async def test_a_resync_request_does_not_unblock_the_auth_pause(
    manager: DeliveryManager,
) -> None:
    """The reboot edge fires on roughly every wake for a sleepy device.

    Clearing the pause in request_config_resync would therefore reopen exactly
    the loop this guards.
    """
    with connection_fails(AuthenticationFailedError("bad key")):
        manager.request_config_resync()
        await manager._deliver()

        manager.request_config_resync()

    assert manager._auth_paused is True
    assert manager._has_pending_work() is False


async def test_every_wake_source_shares_the_auth_gate(
    hass: HomeAssistant, manager: DeliveryManager
) -> None:
    """BLE, mDNS and the post-probe wake all pass through the same gate."""
    device = _uploading_device()
    with connection_fails(AuthenticationFailedError("bad key")):
        manager.request_config_resync()
        await manager._deliver()

    with connects_to(device):
        for source in ("ble", "mdns", "post-probe"):
            manager.notify_device_seen(source)
        await hass.async_block_till_done()

    device.upload_prepared_image.assert_not_awaited()


async def test_a_malformed_stored_key_pauses_without_looping(
    hass: HomeAssistant, entry: MockConfigEntry, manager: DeliveryManager
) -> None:
    """A malformed key returns normally from the drain, so it needs the pause too.

    _drain_once bails out on the _KEY_INVALID sentinel without raising, so no
    except clause ran and the work stayed deliverable, retried on every wake.

    The key is corrupted after setup on purpose: a key that is already
    malformed fails async_setup_entry outright, so the drain-time sentinel is
    only reachable once the entry is loaded.
    """
    device = _uploading_device()
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ENCRYPTION_KEY: "not-32-chars"}
    )
    manager.request_config_resync()
    await manager._deliver()

    assert manager.state.auth_paused is True
    assert manager.state.last_error == "auth"
    assert manager._has_pending_work() is False

    with connects_to(device):
        manager.notify_device_seen()
        await hass.async_block_till_done()
    device.upload_prepared_image.assert_not_awaited()


async def test_submitting_after_an_expiry_reports_auth_not_the_stale_expiry(
    manager: DeliveryManager,
) -> None:
    """Auth failure, then expiry, then new content: the error must read "auth".

    _expire_upload overwrites last_error with "expired", so merely declining to
    clear it on submit would report a stale expiry against brand-new content.
    """
    with connection_fails(AuthenticationFailedError("bad key")):
        _submit(manager)
        await manager._deliver()

        manager._expire_upload(manager._pending_upload)
        assert manager.state.last_error == "expired"

        _submit(manager, device_id="dev2")

    assert manager.state.pending is True
    assert manager.state.auth_paused is True
    assert manager.state.last_error == "auth"
    assert manager._has_pending_work() is False


async def test_auth_paused_reaches_the_entities(
    hass: HomeAssistant, manager: DeliveryManager
) -> None:
    """The flag has to reach a reader, not just sit on the dataclass.

    last_error cannot carry this alone: a paused slot's expiry timer still
    fires and clobbers it with "expired".
    """
    with connection_fails(AuthenticationFailedError("bad key")):
        _submit(manager)
        await manager._deliver()
        # Expiry lands after the pause and overwrites last_error.
        manager._expire_upload(manager._pending_upload)

    assert manager.state.last_error == "expired"
    assert manager.state.auth_paused is True

    sensor = OpenDisplayUpdatePendingSensor(ADDRESS, manager)
    assert sensor.extra_state_attributes["auth_paused"] is True
    assert sensor.extra_state_attributes["last_error"] == "expired"


async def test_drain_config_resync_updates_runtime_and_cache(
    entry: MockConfigEntry, manager: DeliveryManager
) -> None:
    """A resync re-reads firmware and config, and refreshes the dark-start cache."""
    device = _uploading_device()
    device.read_firmware_version = AsyncMock(
        return_value={"major": 1, "minor": 2, "sha": "abc"}
    )
    device.is_flex = False
    device.landing_url = MagicMock(return_value="http://landing")
    device.config = MagicMock()

    with (
        connects_to(device),
        patch("custom_components.opendisplay._write_cache") as cache,
    ):
        manager.request_config_resync()
        await manager._deliver()

    assert entry.runtime_data.config_resync_pending is False
    assert manager._pending_config_resync is False
    assert entry.runtime_data.firmware == {"major": 1, "minor": 2, "sha": "abc"}
    cache.assert_called_once()


async def test_shutdown_detaches_from_wakes_and_cancels_the_expiry(
    hass: HomeAssistant, manager: DeliveryManager
) -> None:
    """After shutdown the manager stops reacting to the device entirely.

    Both halves matter on unload: an advertisement must no longer start a
    drain, and the queue-timeout timer must not outlive the entry.
    """
    expired = async_capture_events(hass, EVENT_CONTENT_EXPIRED)
    manager.async_start()
    _submit(manager)

    await manager.async_shutdown()

    device = _uploading_device()
    with connects_to(device):
        inject_bluetooth_service_info(hass, make_v1_service_info())
        await hass.async_block_till_done()
    device.upload_prepared_image.assert_not_awaited()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=25))
    await hass.async_block_till_done()
    assert not expired


# --- what reaches the library ----------------------------------------------


@pytest.mark.parametrize(
    ("options", "blocks_per_ack", "max_queue_size"),
    [
        ({}, DEFAULT_BLOCKS_PER_ACK, DEFAULT_MAX_QUEUE_SIZE),
        ({CONF_BLOCKS_PER_ACK: 4, CONF_MAX_QUEUE_SIZE: 1}, 4, 1),
    ],
    ids=["defaults", "configured"],
)
async def test_drain_threads_the_pipe_options(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    manager: DeliveryManager,
    options: dict,
    blocks_per_ack: int,
    max_queue_size: int,
) -> None:
    """The sliding-window options reach the OpenDisplayDevice constructor."""
    hass.config_entries.async_update_entry(entry, options=options)
    device = _uploading_device()
    with connects_to(device) as constructor:
        _submit(manager)
        await manager._deliver()

    assert constructor.call_args.kwargs["blocks_per_ack"] == blocks_per_ack
    assert constructor.call_args.kwargs["max_queue_size"] == max_queue_size


async def test_drain_passes_state_and_refresh_mode(manager: DeliveryManager) -> None:
    """The queued refresh mode and partial state reach upload_prepared_image.

    py-opendisplay keeps the upload_prepared_image(prepared, refresh_mode=,
    state=) contract for pipe-partial, so this guards that the wake path keeps
    threading both through.
    """
    device = _uploading_device()
    sentinel_state = MagicMock()
    with connects_to(device):
        _submit(manager, refresh_mode=RefreshMode.PARTIAL, partial_state=sentinel_state)
        await manager._deliver()

    call = device.upload_prepared_image.await_args
    assert call.kwargs["state"] is sentinel_state
    assert call.kwargs["refresh_mode"] is RefreshMode.PARTIAL


# --- the per-MAC BLE lock --------------------------------------------------


async def test_drain_holds_the_registry_lock_while_connected(
    manager: DeliveryManager,
) -> None:
    """The drain body runs while the process-global per-MAC lock is held."""
    device = _uploading_device()
    held: dict[str, bool] = {}

    def _factory(**kwargs):
        class _Ctx:
            async def __aenter__(self):
                held["locked"] = async_get_ble_lock(ADDRESS).locked()
                return device

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    with connects_via(_factory):
        _submit(manager)
        await manager._deliver()

    assert held["locked"] is True


async def test_drain_waits_for_a_preheld_registry_lock(
    manager: DeliveryManager, caplog: pytest.LogCaptureFixture
) -> None:
    """A drain queued behind an in-flight BLE operation waits its turn."""
    caplog.set_level(logging.WARNING, logger="custom_components.opendisplay.ble_lock")
    device = _uploading_device()
    order: list[str] = []
    holding = asyncio.Event()
    release = asyncio.Event()

    async def _hold() -> None:
        async with ble_connection(ADDRESS, "external holder"):
            order.append("holder-enter")
            holding.set()
            await release.wait()
            order.append("holder-exit")

    def _factory(**kwargs):
        class _Ctx:
            async def __aenter__(self):
                order.append("drain-connect")
                return device

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    with connects_via(_factory):
        _submit(manager)
        holder = asyncio.create_task(_hold())
        await holding.wait()

        drain = asyncio.create_task(manager._deliver())
        for _ in range(5):
            await asyncio.sleep(0)
        assert "drain-connect" not in order, "the drain connected while blocked"

        release.set()
        await asyncio.gather(holder, drain)

    assert order == ["holder-enter", "holder-exit", "drain-connect"]
    device.upload_prepared_image.assert_awaited_once()
    contention = [
        record
        for record in caplog.records
        if record.name == "custom_components.opendisplay.ble_lock"
        and record.levelno == logging.WARNING
    ]
    assert len(contention) == 1


async def test_a_frame_queued_mid_upload_is_not_dropped(
    hass: HomeAssistant, manager: DeliveryManager
) -> None:
    """Latest-wins must not lose a frame queued while an older one streams."""
    device = _uploading_device()

    async def _upload(*_args, **_kwargs) -> None:
        _submit(manager, device_id="newer")

    device.upload_prepared_image = AsyncMock(side_effect=_upload)
    with connects_to(device):
        _submit(manager, device_id="first")
        await manager._deliver()

    assert manager.state.pending is True
    assert manager._pending_upload is not None
    assert manager._pending_upload.device_id == "newer"
    await manager.async_shutdown()


# --- sleep after delivery --------------------------------------------------


async def test_sleep_is_sent_after_a_delivery_by_default(
    manager: DeliveryManager,
) -> None:
    """A sleepy tag is sent back to sleep once its queued frame has landed."""
    device = _uploading_device()
    with connects_to(device):
        _submit(manager)
        await manager._deliver()

    device.upload_prepared_image.assert_awaited_once()
    device.deep_sleep.assert_awaited_once_with()


async def test_sleep_follows_both_the_upload_and_the_resync(
    manager: DeliveryManager,
) -> None:
    """Sleep comes last: a resync on the same link must not be cut off."""
    device = _resyncing_device()
    calls = MagicMock()
    calls.attach_mock(device.upload_prepared_image, "upload")
    calls.attach_mock(device.read_firmware_version, "resync")
    calls.attach_mock(device.deep_sleep, "sleep")

    with (
        connects_to(device),
        patch("custom_components.opendisplay._write_cache"),
    ):
        _submit(manager)
        manager.request_config_resync()
        await manager._deliver()

    assert [name for name, *_ in calls.mock_calls] == ["upload", "resync", "sleep"]
    assert manager._has_pending_work() is False


@pytest.mark.parametrize(
    "config_entry_options", [{CONF_SLEEP_AFTER_DELIVERY: False}], ids=["off"]
)
async def test_sleep_is_not_sent_when_the_option_is_off(
    manager: DeliveryManager,
) -> None:
    """With the option off the tag idles out its wake window as before."""
    device = _uploading_device()
    with connects_to(device):
        _submit(manager)
        await manager._deliver()

    device.upload_prepared_image.assert_awaited_once()
    device.deep_sleep.assert_not_awaited()


@pytest.mark.parametrize(
    "config_entry_options", [{CONF_SLEEP_MODE: "off"}], ids=["not-sleepy"]
)
async def test_sleep_is_not_sent_to_a_device_that_is_not_sleepy(
    entry: MockConfigEntry, manager: DeliveryManager
) -> None:
    """Only a device treated as deep-sleeping is ever told to sleep."""
    assert entry.runtime_data.sleep_profile.is_sleepy is False
    device = _uploading_device()
    with connects_to(device):
        _submit(manager)
        await manager._deliver()

    device.upload_prepared_image.assert_awaited_once()
    device.deep_sleep.assert_not_awaited()


async def test_sleep_is_not_sent_when_nothing_was_delivered(
    manager: DeliveryManager,
) -> None:
    """A drain that found no work leaves the device alone."""
    device = _uploading_device()
    with connects_to(device):
        await manager._deliver()

    device.upload_prepared_image.assert_not_awaited()
    device.deep_sleep.assert_not_awaited()


async def test_sleep_is_skipped_when_new_content_arrived_mid_drain(
    manager: DeliveryManager,
) -> None:
    """A frame queued during the upload is not stranded for a whole sleep cycle.

    The next advertisement in this same wake window drains it instead.
    """
    device = _uploading_device()

    async def _upload(*_args, **_kwargs) -> None:
        _submit(manager, device_id="newer")

    device.upload_prepared_image = AsyncMock(side_effect=_upload)
    with connects_to(device):
        _submit(manager, device_id="first")
        await manager._deliver()

    device.deep_sleep.assert_not_awaited()
    # The newer frame was not clobbered by the one that just finished.
    assert manager._pending_upload is not None
    assert manager._pending_upload.device_id == "newer"
    assert manager.state.pending is True
    await manager.async_shutdown()


async def test_sleep_is_skipped_when_content_arrives_during_the_resync(
    manager: DeliveryManager,
) -> None:
    """Work queued while the resync runs also keeps the device awake."""
    device = _resyncing_device()
    resync = device.read_firmware_version.return_value

    async def _read_fw() -> dict:
        _submit(manager, device_id="during-resync")
        return resync

    device.read_firmware_version = AsyncMock(side_effect=_read_fw)
    with (
        connects_to(device),
        patch("custom_components.opendisplay._write_cache"),
    ):
        manager.request_config_resync()
        await manager._deliver()

    device.deep_sleep.assert_not_awaited()
    assert manager._pending_upload is not None
    assert manager._pending_upload.device_id == "during-resync"
    await manager.async_shutdown()


@pytest.mark.parametrize(
    "error",
    [
        ProtocolError("deep sleep not supported"),
        BLEConnectionError("link dropped"),
        BLETimeoutError("no ack"),
        OpenDisplayError("nack"),
        RuntimeError("unexpected"),
    ],
    ids=["nack", "disconnect", "timeout", "opendisplay-error", "unexpected"],
)
async def test_a_failing_sleep_does_not_fail_the_delivery(
    hass: HomeAssistant, manager: DeliveryManager, error: Exception
) -> None:
    """The frame already landed; a sleep error must not count as a failure."""
    delivered = async_capture_events(hass, EVENT_CONTENT_DELIVERED)
    device = _uploading_device()
    device.deep_sleep = AsyncMock(side_effect=error)
    with connects_to(device):
        _submit(manager)
        await manager._deliver()
    await hass.async_block_till_done()

    device.deep_sleep.assert_awaited_once()
    assert len(delivered) == 1
    assert manager.state.pending is False
    assert manager.state.last_error is None
    assert manager.state.attempts == 0


async def test_a_hanging_sleep_is_bounded(
    hass: HomeAssistant, manager: DeliveryManager
) -> None:
    """A sleep command that never returns is abandoned, not waited on forever."""
    delivered = async_capture_events(hass, EVENT_CONTENT_DELIVERED)
    device = _uploading_device()

    async def _hang() -> None:
        await asyncio.sleep(3600)

    device.deep_sleep = AsyncMock(side_effect=_hang)
    with (
        connects_to(device),
        patch.object(delivery_mod, "SLEEP_COMMAND_TIMEOUT_S", 0.01),
    ):
        _submit(manager)
        await manager._deliver()
    await hass.async_block_till_done()

    assert len(delivered) == 1
    assert manager.state.last_error is None


@pytest.mark.parametrize(
    "config_entry_data",
    [{CONF_HOST: "1.2.3.4", CONF_PORT: 2446, CONF_TLS: False}],
    ids=["wifi"],
)
async def test_sleep_is_sent_once_after_a_wifi_to_ble_fallback(
    entry: MockConfigEntry, manager: DeliveryManager
) -> None:
    """WiFi delivers the frame, the resync fails over WiFi, BLE finishes it.

    The sleep must go out exactly once, over the BLE link that completed the
    drain, and not over WiFi before the resync had run.
    """
    entry.runtime_data.mdns_last_seen = time.monotonic()
    wifi = _resyncing_device()
    wifi.read_firmware_version = AsyncMock(side_effect=OSError("wifi dropped"))
    ble = _resyncing_device()

    def _factory(**kwargs):
        return _async_ctx(wifi if "host" in kwargs else ble)

    with (
        connects_via(_factory),
        patch("custom_components.opendisplay._write_cache"),
    ):
        _submit(manager)
        manager.request_config_resync()
        await manager._deliver()

    wifi.upload_prepared_image.assert_awaited_once()
    ble.upload_prepared_image.assert_not_awaited()
    ble.read_firmware_version.assert_awaited_once()
    wifi.deep_sleep.assert_not_awaited()
    ble.deep_sleep.assert_awaited_once_with()
