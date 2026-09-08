"""Wake-triggered delivery of queued work to deep-sleeping OpenDisplay devices.

A sleeping ESP32 tag is dark most of the time and reachable only during a short
advertising window after each timer wake. The `DeliveryManager` owns the work
that could not be delivered immediately (a prepared image, a config resync) and
drains it over a single BLE connection the moment the device is seen advertising
again.

Design (see docs/DEEP_SLEEP_IMPLEMENTATION_PLAN_2026-07-06.md, D4):

* Latest-wins per work type — only the newest image survives, matching the
  existing live-upload semantics.
* One connection per wake — the device stays awake while connected, so all
  pending work drains over a single session in priority order (upload first).
* Transport-agnostic trigger — `notify_device_seen(source)` is the entry point;
  today the BLE coordinator calls it, a future WiFi presence tracker could too.
* Memory-only in v1 — a HA restart drops a pending upload (documented).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from opendisplay import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    GlobalConfig,
    OpenDisplayDevice,
    OpenDisplayError,
    PartialState,
    RefreshMode,
)

from .ble_lock import ble_connection
from .const import (
    CONF_BLOCKS_PER_ACK,
    CONF_ENCRYPTION_KEY,
    CONF_MAX_QUEUE_SIZE,
    DEFAULT_BLOCKS_PER_ACK,
    DEFAULT_MAX_QUEUE_SIZE,
    EVENT_CONTENT_CONFIG_MISMATCH,
    EVENT_CONTENT_DELIVERED,
    EVENT_CONTENT_EXPIRED,
    SIGNAL_IMAGE_UPDATED,
    SIGNAL_PENDING_STATE,
)
from .transport import async_run_with_fallback

if TYPE_CHECKING:
    from . import OpenDisplayConfigEntry

_LOGGER = logging.getLogger(__name__)

# Upper bound on a single wake's delivery attempt: connection establishment plus
# the queued work. Bounds one bad wake so it cannot overlap the next. The device
# stays awake while the BLE link is held, so this is sized to let a full image
# stream over a slow link within one wake rather than to cap a healthy transfer.
# Exceeding it is therefore treated as a genuine failure (payload too large / link
# too slow) and surfaced loudly by ``_deliver`` instead of silently retried.
DELIVERY_DEADLINE_S = 600.0

# Give up a queued upload after this many failed wake attempts. Without this cap a
# frame that can never be delivered (unsupported payload, link too slow) would
# retry on every wake until ``queue_timeout`` hours later; instead it is dropped
# with a ``content_expired`` event so the failure surfaces promptly.
MAX_DELIVERY_ATTEMPTS = 5

# Sentinel distinguishing "no key" (None) from "malformed key" during resolve.
_KEY_INVALID = object()


@dataclass(frozen=True)
class DisplayFingerprint:
    """The subset of a display's config that determines image encoding.

    Used to detect drift between the config an image was prepared against and
    the device's current live config, e.g. a `color_scheme` change made
    without a reboot. A reboot-triggered resync would otherwise miss this.
    Deliberately narrow: comparing the whole `GlobalConfig` would false-positive
    on unrelated field changes (LEDs, WiFi, sensors, ...) and drop good uploads.
    """

    pixel_width: int
    pixel_height: int
    color_scheme: int
    panel_ic_type: int
    rotation: int


def display_fingerprint(config: GlobalConfig) -> DisplayFingerprint:
    """Extract the encoding-relevant fingerprint from a device's config."""
    display = config.displays[0]
    return DisplayFingerprint(
        pixel_width=display.pixel_width,
        pixel_height=display.pixel_height,
        color_scheme=display.color_scheme,
        panel_ic_type=display.panel_ic_type,
        rotation=display.rotation,
    )


@dataclass
class PendingUpload:
    """A prepared image queued for delivery at the next wake."""

    # (uncompressed_data, compressed_data or None, processed_image) from
    # prepare_image() — the heavy CPU work is done once, at queue time.
    prepared: tuple[bytes, bytes | None, Any]
    refresh_mode: RefreshMode
    partial_state: PartialState
    use_measured_palettes: bool
    preview_jpeg: bytes
    device_id: str | None
    queued_at: float
    expires_at: float
    # The display config `prepared` was built against — compared against the
    # live device at drain time so a stale-encoded frame is never sent.
    fingerprint: DisplayFingerprint
    attempts: int = 0
    cancel_deadline: CALLBACK_TYPE | None = None


@dataclass(frozen=True)
class DeliveryReceipt:
    """Result of submitting content: whether it was delivered or queued."""

    status: str  # "delivered" | "queued"
    expires_at: float | None


@dataclass(frozen=True)
class DeliverySnapshot:
    """Public delivery state consumed by entities (image, binary sensor)."""

    pending: bool
    queued_at: float | None
    expires_at: float | None
    attempts: int
    last_error: str | None
    # Authoritative "delivery is blocked on authentication" signal. ``last_error``
    # is only best-effort for this: a paused slot's expiry timer still fires and
    # overwrites it with "expired".
    auth_paused: bool = False


class DeliveryManager:
    """Owns queued work for one config entry and delivers it at the next wake."""

    def __init__(self, hass: HomeAssistant, entry: OpenDisplayConfigEntry) -> None:
        """Initialize the delivery manager."""
        self._hass = hass
        self._entry = entry
        runtime = entry.runtime_data
        self._coordinator = runtime.coordinator
        self._profile = runtime.sleep_profile
        self._address: str = entry.unique_id or ""

        self._pending_upload: PendingUpload | None = None
        self._pending_config_resync: bool = False
        self._last_error: str | None = None
        # Terminal state for authentication failures. The device rejects every
        # further attempt identically, and delivery is advertisement-driven with
        # no throttle, so without this a pending config resync would reconnect
        # once per advertisement forever (issue #91). Covers *both* work types
        # and is cleared only by an entry reload, which rebuilds this manager.
        self._auth_paused: bool = False

        self._delivering: bool = False
        self._delivery_task: asyncio.Task[None] | None = None
        self._unsub_device_seen: CALLBACK_TYPE | None = None

    # -- lifecycle ----------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Subscribe to wake advertisements."""
        self._unsub_device_seen = self._coordinator.async_subscribe_device_seen(
            self._on_device_seen
        )

    async def async_shutdown(self) -> None:
        """Cancel timers and any in-flight delivery, and unsubscribe."""
        if self._unsub_device_seen is not None:
            self._unsub_device_seen()
            self._unsub_device_seen = None
        if self._pending_upload is not None and self._pending_upload.cancel_deadline:
            self._pending_upload.cancel_deadline()
            self._pending_upload.cancel_deadline = None
        if self._delivery_task is not None and not self._delivery_task.done():
            self._delivery_task.cancel()
            try:
                await self._delivery_task
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOGGER.debug("Delivery task raised during shutdown", exc_info=True)
        self._delivery_task = None
        self._delivering = False

    # -- public state -------------------------------------------------------

    @property
    def state(self) -> DeliverySnapshot:
        """Return the current pending-delivery state for entities."""
        upload = self._pending_upload
        return DeliverySnapshot(
            pending=upload is not None,
            queued_at=upload.queued_at if upload else None,
            expires_at=upload.expires_at if upload else None,
            attempts=upload.attempts if upload else 0,
            last_error=self._last_error,
            auth_paused=self._auth_paused,
        )

    # -- submission ---------------------------------------------------------

    @callback
    def submit_upload(
        self,
        *,
        prepared: tuple[bytes, bytes | None, Any],
        refresh_mode: RefreshMode,
        partial_state: PartialState,
        use_measured_palettes: bool,
        preview_jpeg: bytes,
        device_id: str | None,
        fingerprint: DisplayFingerprint,
    ) -> DeliveryReceipt:
        """Queue a prepared image for delivery at the next wake (latest-wins)."""
        now = time.time()
        # Latest-wins: drop any previously queued image and its deadline timer.
        if self._pending_upload is not None and self._pending_upload.cancel_deadline:
            self._pending_upload.cancel_deadline()
        expires_at = now + self._profile.queue_timeout_s
        slot = PendingUpload(
            prepared=prepared,
            refresh_mode=refresh_mode,
            partial_state=partial_state,
            use_measured_palettes=use_measured_palettes,
            preview_jpeg=preview_jpeg,
            device_id=device_id,
            queued_at=now,
            expires_at=expires_at,
            fingerprint=fingerprint,
        )
        self._schedule_expiry(slot)
        self._pending_upload = slot
        # Queuing new content must not make a paused manager look healthy, and
        # must not resume connection attempts: the pause is automation-proof by
        # design (a frame-changing automation would otherwise re-arm the storm
        # before every advertisement). Restore "auth" rather than merely keeping
        # the current value -- the previous slot's expiry timer may have already
        # overwritten it with "expired", which would otherwise be reported
        # against this brand-new upload.
        self._last_error = "auth" if self._auth_paused else None
        # Show the intended frame immediately (the image entity now reflects
        # what will be delivered, not what is on the panel).
        async_dispatcher_send(
            self._hass, f"{SIGNAL_IMAGE_UPDATED}_{self._address}", preview_jpeg
        )
        self._notify_state()
        return DeliveryReceipt(status="queued", expires_at=expires_at)

    @callback
    def request_config_resync(self) -> None:
        """Request a firmware/config re-read at the next wake.

        Deliberately does *not* clear ``_auth_paused``: this is called
        automatically on every advertised reboot edge for sleepy devices, so
        clearing here would reopen the per-advertisement loop of issue #91.
        """
        self._pending_config_resync = True
        self._entry.runtime_data.config_resync_pending = True

    # -- wake handling ------------------------------------------------------

    @callback
    def _on_device_seen(self) -> None:
        """Coordinator callback: the device advertised (it is awake now)."""
        self.notify_device_seen("ble")

    @callback
    def notify_device_seen(self, source: str = "ble") -> None:
        """Start a delivery drain if there is pending work and none in flight."""
        if self._delivering or not self._has_pending_work():
            return
        _LOGGER.debug("%s: device seen (%s); starting delivery", self._address, source)
        self._delivering = True
        self._delivery_task = self._entry.async_create_background_task(
            self._hass, self._deliver(), f"opendisplay_delivery_{self._address}"
        )

    def _has_pending_work(self) -> bool:
        """Return True if there is deliverable work queued."""
        if self._auth_paused:
            return False
        return self._pending_upload is not None or self._pending_config_resync

    # -- delivery drain -----------------------------------------------------

    async def _deliver(self) -> None:
        """Open one connection and drain all pending slots in priority order."""
        try:
            await self._drain_once()
        except asyncio.CancelledError:
            raise
        except TimeoutError as err:
            # The whole drain exceeded DELIVERY_DEADLINE_S. Because the device
            # stays awake while connected, hitting this ceiling is not a missed
            # wake but a genuine failure: the payload is too large or the link too
            # slow to complete in one wake. Record it, log loudly, and raise so it
            # is not swallowed as a routine retry. (py-opendisplay wraps its own
            # read/connect timeouts as BLETimeoutError, so a bare TimeoutError here
            # can only be the asyncio.timeout(DELIVERY_DEADLINE_S) firing.)
            self._register_attempt_failure(
                f"delivery deadline exceeded ({DELIVERY_DEADLINE_S:.0f}s)"
            )
            _LOGGER.error(
                "%s: delivery aborted after exceeding the %.0f s deadline; the "
                "image is likely too large to stream within a single wake",
                self._address,
                DELIVERY_DEADLINE_S,
            )
            raise HomeAssistantError(
                f"OpenDisplay {self._address}: delivery timed out after "
                f"{DELIVERY_DEADLINE_S:.0f}s (image too large or BLE link too slow "
                "to finish in one wake)"
            ) from err
        except (BLEConnectionError, BLETimeoutError) as err:
            # Device slept mid-connect or the wake window was missed; keep the
            # work queued and try again on the next wake.
            self._register_attempt_failure(str(err))
        except AuthenticationFailedError, AuthenticationRequiredError:
            # Bad/rotated/absent key. Pausing only the upload would leave a
            # pending config resync deliverable, and the next advertisement would
            # start another identical, doomed session (issue #91), so the pause is
            # manager-wide.
            _LOGGER.warning("%s: delivery auth failed; starting reauth", self._address)
            self._pause_for_auth(start_reauth=True)
        except OpenDisplayError as err:
            _LOGGER.warning("%s: delivery failed: %s", self._address, err)
            self._register_attempt_failure(str(err))
        finally:
            self._delivering = False
            self._delivery_task = None

    async def _drain_once(self) -> None:
        """Run the connection + drain body (see `_deliver` for error handling)."""
        key = self._resolve_key()
        if key is _KEY_INVALID:
            # Malformed stored key; ``_resolve_key`` already started reauth. This
            # returns normally rather than raising, so without pausing here the
            # work stays deliverable and every advertisement retries it — the
            # same loop as the auth handler above, by a second route.
            self._pause_for_auth(start_reauth=False)
            return

        runtime = self._entry.runtime_data
        upload = self._pending_upload
        use_measured = upload.use_measured_palettes if upload else True
        base_kwargs: dict[str, Any] = {
            "config": runtime.device_config,
            "use_measured_palettes": use_measured,
            "encryption_key": key,
            "blocks_per_ack": self._entry.options.get(
                CONF_BLOCKS_PER_ACK, DEFAULT_BLOCKS_PER_ACK
            ),
            "max_queue_size": self._entry.options.get(
                CONF_MAX_QUEUE_SIZE, DEFAULT_MAX_QUEUE_SIZE
            ),
        }

        async def _run(device: OpenDisplayDevice) -> None:
            # Always refresh first, unconditionally: a config change made
            # without a device reboot is otherwise never picked up (the only
            # other trigger is the reboot-advertised resync flag), and
            # draining a stale-encoded upload before a pending resync ran was
            # the original ordering bug this replaces. Local import avoids an
            # import cycle (__init__ imports this module).
            from . import _refresh_config_from_device

            live_config = await _refresh_config_from_device(
                self._hass, self._entry, device
            )
            if live_config is not None:
                self._pending_config_resync = False

            # Re-read pending state each invocation: if a WiFi attempt uploaded
            # then failed on resync, the BLE fallback re-runs this and must not
            # re-upload an already-delivered frame (``_drain_upload`` clears it).
            pending = self._pending_upload
            if pending is None:
                return
            if live_config is not None:
                live_fingerprint = display_fingerprint(live_config)
                if live_fingerprint != pending.fingerprint:
                    self._drop_mismatched_upload(pending, live_fingerprint)
                    return
            await self._drain_upload(device, pending)

        # Prefer WiFi when the entry has a fresh mDNS host; fall back to BLE on any
        # WiFi failure. All inside the per-MAC lock (MAC-keyed, transport-neutral).
        # A missing connectable BLE device raises BLEConnectionError, which
        # ``_deliver`` turns into a retry-next-wake attempt failure — the same
        # outcome as the previous inline "device not connectable" check.
        async with (
            ble_connection(self._address, "queued content delivery"),
            asyncio.timeout(DELIVERY_DEADLINE_S),
        ):
            await async_run_with_fallback(
                self._hass,
                self._entry,
                _run,
                base_kwargs=base_kwargs,
                ble_unavailable=lambda: BLEConnectionError("device not connectable"),
            )

    async def _drain_upload(
        self, device: OpenDisplayDevice, upload: PendingUpload
    ) -> None:
        """Send the queued prepared image and fire the delivered event."""
        await device.upload_prepared_image(
            upload.prepared,
            refresh_mode=upload.refresh_mode,
            state=upload.partial_state,
        )
        if upload.cancel_deadline:
            upload.cancel_deadline()
        self._pending_upload = None
        self._last_error = None
        # Bump the image entity's timestamp to when the frame actually landed.
        async_dispatcher_send(
            self._hass, f"{SIGNAL_IMAGE_UPDATED}_{self._address}", upload.preview_jpeg
        )
        self._fire_content_event(EVENT_CONTENT_DELIVERED, upload)
        self._notify_state()
        _LOGGER.info("%s: queued content delivered", self._address)

    # -- expiry -------------------------------------------------------------

    @callback
    def _schedule_expiry(self, slot: PendingUpload) -> None:
        """Arm the deadline timer that expires this queued upload."""

        @callback
        def _expired(_now: Any) -> None:
            # Guard against a stale timer firing after latest-wins replaced it.
            if self._pending_upload is slot:
                self._expire_upload(slot)

        slot.cancel_deadline = async_call_later(
            self._hass, self._profile.queue_timeout_s, _expired
        )

    @callback
    def _expire_upload(self, slot: PendingUpload) -> None:
        """Drop an expired queued upload and fire the expired event."""
        self._pending_upload = None
        self._last_error = "expired"
        _LOGGER.warning(
            "%s: queued content expired after %s h without a wake",
            self._address,
            self._profile.queue_timeout_hours,
        )
        self._fire_content_event(EVENT_CONTENT_EXPIRED, slot)
        self._notify_state()

    @callback
    def _give_up_upload(self, slot: PendingUpload, reason: str) -> None:
        """Drop an upload that has exhausted ``MAX_DELIVERY_ATTEMPTS``.

        Cancels the pending expiry timer, clears the slot, and fires the same
        ``content_expired`` event as time-based expiry — the payload's
        ``attempts == MAX_DELIVERY_ATTEMPTS`` distinguishes this cause, and the
        entity's ``last_error`` reads ``"failed"`` rather than ``"expired"``.
        """
        if slot.cancel_deadline:
            slot.cancel_deadline()
            slot.cancel_deadline = None
        self._pending_upload = None
        self._last_error = "failed"
        _LOGGER.error(
            "%s: giving up queued content after %s failed delivery attempts (%s)",
            self._address,
            slot.attempts,
            reason,
        )
        self._fire_content_event(EVENT_CONTENT_EXPIRED, slot)
        self._notify_state()

    @callback
    def _drop_mismatched_upload(
        self, slot: PendingUpload, live_fingerprint: DisplayFingerprint
    ) -> None:
        """Drop a queued upload whose fingerprint no longer matches the live device.

        The device's display config changed (e.g. `color_scheme` edited
        without a reboot) since this frame was prepared, so its encoded bytes
        no longer match what the panel expects — sending it would corrupt the
        display. Unlike a live/immediate upload, there is no source image
        available here to re-render from (``PendingUpload`` only retains the
        already-encoded/dithered result), so the safest option is to drop it
        and let the next scheduled push re-prepare against the current config.
        """
        if slot.cancel_deadline:
            slot.cancel_deadline()
            slot.cancel_deadline = None
        self._pending_upload = None
        self._last_error = "config_mismatch"
        _LOGGER.warning(
            "%s: dropping queued upload - device config changed since the image "
            "was prepared (prepared for %s, device is now %s)",
            self._address,
            slot.fingerprint,
            live_fingerprint,
        )
        self._fire_content_event(EVENT_CONTENT_CONFIG_MISMATCH, slot)
        self._notify_state()

    # -- helpers ------------------------------------------------------------

    @callback
    def _pause_for_auth(self, *, start_reauth: bool) -> None:
        """Enter the manager-wide terminal state for an authentication failure.

        Both entry points (the ``_deliver`` handler and the malformed-key path in
        ``_drain_once``) must set the flag, record the error and notify entities,
        so they share this helper rather than repeating it. ``start_reauth`` is
        False where the caller has already started the flow.
        """
        self._auth_paused = True
        self._last_error = "auth"
        if start_reauth:
            self._entry.async_start_reauth(self._hass)
        self._notify_state()

    def _register_attempt_failure(self, reason: str) -> None:
        """Record a failed wake attempt and keep the work queued.

        After ``MAX_DELIVERY_ATTEMPTS`` failures the upload is given up and
        dropped (``_give_up_upload``) so a frame that can never be delivered does
        not retry on every wake until ``queue_timeout``.
        """
        upload = self._pending_upload
        if upload is not None:
            upload.attempts += 1
            if upload.attempts >= MAX_DELIVERY_ATTEMPTS:
                self._give_up_upload(upload, reason)
                return
        self._last_error = reason
        _LOGGER.debug(
            "%s: delivery attempt %s/%s failed (%s); will retry next wake",
            self._address,
            upload.attempts if upload is not None else 0,
            MAX_DELIVERY_ATTEMPTS,
            reason,
        )
        self._notify_state()

    def _resolve_key(self) -> bytes | None | object:
        """Return the encryption key bytes, None, or a sentinel on bad format."""
        raw = self._entry.data.get(CONF_ENCRYPTION_KEY)
        if raw is None:
            return None
        if len(raw) != 32:
            _LOGGER.error(
                "%s: stored encryption key is malformed (bad length); reauthentication required",
                self._address,
            )
            self._entry.async_start_reauth(self._hass)
            return _KEY_INVALID
        try:
            return bytes.fromhex(raw)
        except ValueError:
            _LOGGER.error(
                "%s: stored encryption key is malformed (not hex); reauthentication required",
                self._address,
            )
            self._entry.async_start_reauth(self._hass)
            return _KEY_INVALID

    @callback
    def _notify_state(self) -> None:
        """Push the current pending state to entities."""
        async_dispatcher_send(
            self._hass, f"{SIGNAL_PENDING_STATE}_{self._address}", self.state
        )

    def _fire_content_event(self, event: str, slot: PendingUpload) -> None:
        """Fire a content delivered/expired bus event for automations."""
        device_id = slot.device_id or self._device_id()
        self._hass.bus.async_fire(
            event,
            {
                "device_id": device_id,
                "queued_at": slot.queued_at,
                "attempts": slot.attempts,
            },
        )

    def _device_id(self) -> str | None:
        """Resolve this entry's device registry id, if any."""
        device = dr.async_get(self._hass).async_get_device(
            connections={(CONNECTION_BLUETOOTH, self._address)}
        )
        return device.id if device else None
