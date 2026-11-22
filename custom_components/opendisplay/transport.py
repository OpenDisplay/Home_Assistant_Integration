"""Transport resolution for OpenDisplay deliveries (BLE vs WiFi/LAN).

A single OpenDisplay tag exposes at most one BLE link *and*, when WiFi-enabled,
a TCP/LAN endpoint discovered via mDNS (protocol 2.2 SECTION 9). Both transports
address the same device by its BLE MAC (the config entry's ``unique_id``), so the
process-global per-MAC lock in ``ble_lock.py`` serializes them transparently —
this module never touches the lock; callers hold it around the whole delivery.

Resolution rule (H3): prefer WiFi when the entry carries a ``CONF_HOST`` *and* the
device was seen via mDNS within ``MDNS_FRESHNESS_WINDOW`` (fed by the zeroconf
config-flow step, see ``note_mdns_seen``); otherwise use BLE. Any WiFi failure
(unreachable / connect / TLS handshake / mid-transfer / stale port) falls back to
BLE for that same delivery — a hard requirement — via ``async_run_with_fallback``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant

from opendisplay import (
    BLETimeoutError,
    OpenDisplayConnectionError,
    OpenDisplayDevice,
    OpenDisplayTimeoutError,
    connect,
)

from .const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TLS,
    DEFAULT_LAN_PORT,
    MDNS_FRESHNESS_WINDOW_S,
)

if TYPE_CHECKING:
    from . import OpenDisplayConfigEntry

_LOGGER = logging.getLogger(__name__)

# Transport labels recorded on runtime data / surfaced in diagnostics.
TRANSPORT_WIFI = "wifi"
TRANSPORT_BLE = "ble"

# Exceptions from a WiFi attempt that must trigger the BLE fallback. The neutral
# py-opendisplay superclasses cover connect/read failures on any transport;
# ``OSError`` (and its ``ssl.SSLError`` subclass) covers raw socket / TLS
# handshake failures raised before the library wraps them. A genuine
# ``ProtocolError`` is deliberately *not* here — it is a real device fault, not a
# transport problem, and re-running it over BLE would just fail again.
WIFI_FALLBACK_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OpenDisplayConnectionError,
    OpenDisplayTimeoutError,
    OSError,
)


@dataclass(frozen=True)
class ResolvedTransport:
    """The transport chosen for one delivery attempt."""

    use_wifi: bool
    host: str | None
    port: int
    tls: bool


def resolve_transport(entry: OpenDisplayConfigEntry) -> ResolvedTransport:
    """Decide which transport to use for the next connection to ``entry``.

    WiFi is preferred only when the entry stores a host *and* the device has been
    seen via mDNS recently (``MDNS_FRESHNESS_WINDOW``); otherwise BLE. An entry
    with no ``CONF_HOST`` (every pre-WiFi entry) always resolves to BLE, so
    existing installs need no migration.
    """
    data = entry.data
    host = data.get(CONF_HOST)
    port = int(data.get(CONF_PORT) or DEFAULT_LAN_PORT)
    tls = bool(data.get(CONF_TLS))
    if not host:
        return ResolvedTransport(False, None, port, tls)

    last_seen = _mdns_last_seen(entry)
    fresh = (
        last_seen is not None
        and (time.monotonic() - last_seen) <= MDNS_FRESHNESS_WINDOW_S
    )
    if not fresh:
        _LOGGER.debug(
            "%s: host %s known but mDNS presence stale/absent; using BLE",
            entry.unique_id,
            host,
        )
    return ResolvedTransport(fresh, host, port, tls)


def _mdns_last_seen(entry: OpenDisplayConfigEntry) -> float | None:
    """Return the monotonic timestamp of the last mDNS sighting, or None."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return None
    return getattr(runtime, "mdns_last_seen", None)


def note_mdns_seen(entry: OpenDisplayConfigEntry) -> None:
    """Record that ``entry``'s device was just seen via mDNS.

    Called from the zeroconf config-flow step for an already-configured entry so
    the resolver can prefer WiFi, and so the delivery manager treats the mDNS
    sighting as a wake (union of BLE-advert and mDNS presence).
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    try:
        runtime.mdns_last_seen = time.monotonic()
    except AttributeError:  # pragma: no cover - runtime is always a mutable dataclass
        return
    delivery = getattr(runtime, "delivery", None)
    if delivery is not None:
        delivery.notify_device_seen("mdns")


def record_last_transport(entry: OpenDisplayConfigEntry, transport: str) -> None:
    """Store the transport a completed delivery used (surfaced in diagnostics)."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    with contextlib.suppress(AttributeError):  # pragma: no cover
        runtime.last_transport = transport


async def _connect_and_run(
    action: Callable[[OpenDisplayDevice], Awaitable[None]],
    encryption_key: bytes | None,
    connect_deadline: float | None,
    **addressing: Any,
) -> None:
    """Connect with ``opendisplay.connect()`` and run ``action`` on the device.

    ``connect_deadline`` bounds only entering the connection (link, firmware
    version, identity, security, config), not ``action``. A breach is raised as
    ``BLETimeoutError`` so callers treat it like any other connect timeout.
    """
    async with contextlib.AsyncExitStack() as stack:
        deadline = asyncio.timeout(connect_deadline)
        try:
            async with deadline:
                device = await stack.enter_async_context(
                    connect(encryption_key=encryption_key, **addressing)
                )
        except TimeoutError as err:
            if not deadline.expired():
                raise
            raise BLETimeoutError(
                f"Connecting took longer than {connect_deadline:.0f}s"
            ) from err
        await action(device.legacy)


async def async_run_with_fallback(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    action: Callable[[OpenDisplayDevice], Awaitable[None]],
    *,
    encryption_key: bytes | None,
    ble_unavailable: Callable[[], BaseException],
    connect_deadline: float | None = None,
) -> str:
    """Open a device and run ``action`` over the resolved transport.

    Tries WiFi first when :func:`resolve_transport` selects it; on any WiFi
    transport failure (``WIFI_FALLBACK_EXCEPTIONS``) it falls back to BLE and
    re-runs the *same* ``action`` over BLE before giving up. Returns the label of
    the transport that completed the delivery.

    Must be called inside the per-MAC ``ble_connection`` lock context (the lock is
    MAC-keyed, hence transport-neutral). ``connect_deadline`` bounds each connect
    (the probe path uses it to give up quickly); ``None`` leaves the library's own
    budget. ``ble_unavailable`` builds the exception to raise when the BLE path is
    selected (or fallen back to) but no connectable device exists — each caller
    supplies its own (``_DeviceUnavailable`` / a translated ``device_not_found``).
    """
    address = entry.unique_id
    assert address is not None
    resolved = resolve_transport(entry)

    if resolved.use_wifi:
        try:
            await _connect_and_run(
                action,
                encryption_key,
                connect_deadline,
                host=resolved.host,
                port=resolved.port,
                tls=resolved.tls,
            )
        except WIFI_FALLBACK_EXCEPTIONS as err:
            _LOGGER.warning(
                "%s: WiFi delivery to %s:%s failed (%s); falling back to BLE",
                address,
                resolved.host,
                resolved.port,
                err,
            )
        else:
            record_last_transport(entry, TRANSPORT_WIFI)
            return TRANSPORT_WIFI

    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ble_unavailable()

    await _connect_and_run(
        action,
        encryption_key,
        connect_deadline,
        address=address,
        ble_device=ble_device,
    )
    record_last_transport(entry, TRANSPORT_BLE)
    return TRANSPORT_BLE
