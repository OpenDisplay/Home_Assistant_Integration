"""Unit tests for the _async_send_image sleep gate and probe-before-queue.

Mirrors the test_delivery.py approach: no Home Assistant test harness; the
service module's HA touchpoints (``prepare_image``, ``_pil_to_jpeg``,
``async_dispatcher_send``) are patched in the services module namespace, while the
connection touchpoints (``async_ble_device_from_address`` and ``OpenDisplayDevice``)
are patched in the transport module namespace, where the resolver now calls them.
"""

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from opendisplay import BLEConnectionError, RefreshMode, DitherMode
from PIL import Image as PILImage
import pytest
import voluptuous as vol

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.opendisplay import services as services_mod
from custom_components.opendisplay import transport as transport_mod
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
    SCHEMA_PLAY_MELODY,
    _async_play_melody,
    _async_send_image,
    normalize_drawcustom_elements,
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
            transport_mod, "async_ble_device_from_address", return_value=ble_device
        ),
        patch.object(transport_mod, "OpenDisplayDevice", od_factory),
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


# --------------------------------------------------------------------------
# play_melody service
#
# Same no-HA-harness approach: the handler's touchpoints
# (``_get_entry_for_device``, ``_raise_if_sleeping``, ``_async_connect_and_run``)
# are patched in the services module namespace; the config is byte-compared via
# ``BuzzerActivateConfig.melody(...).to_bytes()`` against wire bytes derived from
# the protocol reference (§7.2/§7.4).
# --------------------------------------------------------------------------


def _melody_env(buzzers=(object(),)):
    """Return an (entry, device, call-factory) trio for play_melody tests."""
    entry = MagicMock()
    entry.runtime_data.device_config.buzzers = list(buzzers)
    device = MagicMock()
    device.activate_buzzer = AsyncMock()

    def make_call(**data):
        payload = {
            "device_id": "dev-1",
            "instance": 0,
            "notes": "A4:200",
            "tempo": 120,
            "repeats": 1,
            "default_note_ms": 200,
        }
        payload.update(data)
        call = MagicMock()
        call.data = payload  # a real dict, so __getitem__/.get behave
        call.hass = MagicMock()
        return call

    return entry, device, make_call


def _play_patches(entry, device, sleeping_exc=None):
    """Patch the handler's three touchpoints; drive the captured callback."""

    async def _fake_connect(hass, e, cb):
        await cb(device)

    def _fake_sleeping(e, device_id):
        if sleeping_exc is not None:
            raise sleeping_exc

    return (
        patch.object(services_mod, "_get_entry_for_device", return_value=entry),
        patch.object(services_mod, "_raise_if_sleeping", side_effect=_fake_sleeping),
        patch.object(services_mod, "_async_connect_and_run", side_effect=_fake_connect),
    )


@pytest.mark.asyncio
async def test_play_melody_happy_path():
    """Valid notes reach the device as one activate_buzzer call with wire-exact bytes."""
    entry, device, make_call = _melody_env()
    call = make_call(instance=2, notes="A4:200 R:50 144:200")
    p1, p2, p3 = _play_patches(entry, device)
    with p1, p2, p3:
        await _async_play_melody(call)

    device.activate_buzzer.assert_awaited_once()
    (instance, config), _ = device.activate_buzzer.await_args
    assert instance == 2
    # A4->78/40u, R->00/10u, 144->90/40u; outer_repeats=1, one pattern (§7.2).
    assert config.to_bytes() == bytes.fromhex("0101037828000a9028")


@pytest.mark.asyncio
async def test_play_melody_default_length_and_tempo():
    """tempo + default_length plumb through: terse Twinkle == explicit-fraction Twinkle."""
    entry, device, make_call = _melody_env()
    call = make_call(notes="C5 C5 G5 G5 A5 A5 G5/2", tempo=120, default_length=4)
    p1, p2, p3 = _play_patches(entry, device)
    with p1, p2, p3:
        await _async_play_melody(call)

    (_, config), _ = device.activate_buzzer.await_args
    # 120 BPM: quarter=500ms=0x64, half=1000ms=0xC8; C5=126, G5=140, A5=144 (§7.4).
    assert config.to_bytes() == bytes.fromhex("0101077e647e648c648c64906490648cc8")


@pytest.mark.asyncio
async def test_play_melody_default_note_ms_and_repeats():
    """default_note_ms sets unmarked durations; repeats maps to the wire outer_repeat."""
    entry, device, make_call = _melody_env()
    call = make_call(notes="A4", default_note_ms=100, repeats=2)
    p1, p2, p3 = _play_patches(entry, device)
    with p1, p2, p3:
        await _async_play_melody(call)

    (_, config), _ = device.activate_buzzer.await_args
    # A4->0x78, 100ms=20 units=0x14, outer_repeats=2.
    assert config.to_bytes() == bytes.fromhex("0201017814")


@pytest.mark.asyncio
async def test_play_melody_no_buzzers_raises():
    """A device without a buzzer rejects the call before any BLE work."""
    entry, device, make_call = _melody_env(buzzers=())
    call = make_call()
    p1, p2, p3 = _play_patches(entry, device)
    with p1, p2, p3, pytest.raises(ServiceValidationError) as excinfo:
        await _async_play_melody(call)
    assert excinfo.value.translation_key == "no_buzzers"
    device.activate_buzzer.assert_not_awaited()


@pytest.mark.asyncio
async def test_play_melody_sleeping_raises():
    """A provably-asleep device fails fast at the sleep gate."""
    entry, device, make_call = _melody_env()
    call = make_call()
    p1, p2, p3 = _play_patches(
        entry, device, sleeping_exc=HomeAssistantError("asleep")
    )
    with p1, p2, p3, pytest.raises(HomeAssistantError):
        await _async_play_melody(call)
    device.activate_buzzer.assert_not_awaited()


@pytest.mark.asyncio
async def test_play_melody_handler_time_overflow_raises():
    """Tempo-dependent overflow (uncatchable at schema time) surfaces here.

    ``C5/1`` (a whole note) at 40 BPM resolves to 6000 ms > 1275 ms; the parser
    raises ValueError, which the handler re-raises as ``invalid_melody``.
    """
    entry, device, make_call = _melody_env()
    call = make_call(notes="C5/1", tempo=40)
    p1, p2, p3 = _play_patches(entry, device)
    with p1, p2, p3, pytest.raises(ServiceValidationError) as excinfo:
        await _async_play_melody(call)
    assert excinfo.value.translation_key == "invalid_melody"
    assert "1275" in excinfo.value.translation_placeholders["error"]
    device.activate_buzzer.assert_not_awaited()


def test_schema_play_melody_accepts_valid():
    """The schema fills defaults and accepts a well-formed melody string."""
    data = SCHEMA_PLAY_MELODY({"device_id": "dev-1", "notes": "C5 C5 G5/2"})
    assert data["instance"] == 0
    assert data["tempo"] == 120
    assert data["repeats"] == 1
    assert data["default_note_ms"] == 200
    assert "default_length" not in data
    assert data["notes"] == "C5 C5 G5/2"


def test_schema_play_melody_accepts_default_length():
    """default_length accepts the select's string value (coerced to int)."""
    data = SCHEMA_PLAY_MELODY(
        {"device_id": "dev-1", "notes": "C5 C5", "default_length": "4"}
    )
    assert data["default_length"] == 4


def test_schema_play_melody_rejects_unknown_note():
    """A bad note letter is rejected by _valid_melody at schema time."""
    with pytest.raises(vol.Invalid):
        SCHEMA_PLAY_MELODY({"device_id": "dev-1", "notes": "H4:100"})


def test_schema_play_melody_rejects_pipe():
    """The reserved multi-pattern separator is rejected at schema time."""
    with pytest.raises(vol.Invalid):
        SCHEMA_PLAY_MELODY({"device_id": "dev-1", "notes": "A4 | C5"})


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


@pytest.mark.asyncio
@pytest.mark.parametrize("measured", [True, False])
async def test_send_image_forwards_measured_palette_choice(measured):
    """The service's measured_palette option must reach prepare_image.

    Regression: ``_async_send_image`` accepted ``use_measured_palettes`` but
    never forwarded it, so ``prepare_image``'s default (True) silently won and
    ``measured_palette: false`` in drawcustom/upload_image was a no-op — flat
    UI colors were always quantized against the measured panel palette.
    """
    hass, entry, _, _ = _make_env(last_seen=time.time())  # awake -> live path
    device = MagicMock()
    device.upload_prepared_image = AsyncMock()
    od = _device_ctx_factory(device=device)
    p1, p2, p3, p4, p5 = _patches(od)
    img = PILImage.new("RGB", (1, 1))
    with p1 as prep, p2, p3, p4, p5:
        await _async_send_image(
            hass,
            entry,
            img,
            dither_mode=DitherMode.NONE,
            refresh_mode=RefreshMode.FULL,
            use_measured_palettes=measured,
        )
    assert prep.call_args.kwargs.get("use_measured_palettes") is measured


def test_normalize_drawcustom_elements_repairs_yaml11_y_key():
    """PyYAML turns bare ``y:`` into boolean True — restore string key ``y`` (#97)."""
    repaired = normalize_drawcustom_elements(
        [
            {"type": "text", "value": "a", "x": 10, True: 10, "size": 32},
            {"type": "text", "value": "b", "x": 10, "y": 250, "size": 32},
            {
                "type": "plot",
                "x_start": 0,
                True: 0,
                "data": [{"x": 1, True: 2}],
            },
        ]
    )
    assert repaired[0]["y"] == 10
    assert True not in repaired[0]
    assert repaired[1]["y"] == 250
    assert repaired[2]["y"] == 0
    assert repaired[2]["data"][0]["y"] == 2
