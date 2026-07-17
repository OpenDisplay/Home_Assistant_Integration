"""Process-global per-MAC BLE connection lock for OpenDisplay tags.

An OpenDisplay tag exposes a single BLE link and the library holds no
per-address lock, so every host-side connect to a given MAC must be serialized
or overlapping connects race and wedge BlueZ (the dongle-only config-read
investigation, 2026-07-16). The lock has to be *process-global and keyed by
MAC* rather than per config entry, because two of the connect sites run before
(or without) a config entry: the config-flow probe and the entry-setup
interrogation. A per-entry lock leaves those two unguarded against each other
and against a reloading entry.

``WeakValueDictionary`` here is **load-bearing, not hygiene**: an
``asyncio.Lock`` binds to the event loop that first acquires it, and
pytest-asyncio spins up a fresh loop per test while the test modules reuse one
``ADDRESS`` constant. A plain ``dict`` would cache a lock bound to a now-dead
loop and raise ``RuntimeError`` on the next test's acquire. Weak references let
the entry drop the moment nothing holds the lock, so each test's first
``async_get_ble_lock`` mints a lock bound to that test's live loop. In
production the same weakness is harmless: a lock lingers only while a connect
holds it and is otherwise collected.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from weakref import WeakValueDictionary

from homeassistant.helpers.device_registry import format_mac

_LOGGER = logging.getLogger(__name__)

# Normalized MAC -> lock serializing every BLE connect to that tag. Weak values
# so a lock is collected once nothing references it (see module docstring).
_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
# Normalized MAC -> purpose string of the operation currently holding the lock,
# used only to name the holder in the contention WARNING.
_HOLDERS: dict[str, str] = {}


def async_get_ble_lock(address: str) -> asyncio.Lock:
    """Return the shared BLE lock for ``address``, creating it on first use.

    ``address`` is normalized with ``format_mac`` because config_flow keys off
    raw discovery addresses while everything else uses ``entry.unique_id``; both
    must map to the same lock. There is no ``await`` between the lookup and the
    insert, so this is atomic on the event loop.
    """
    key = format_mac(address)
    if (lock := _LOCKS.get(key)) is None:
        _LOCKS[key] = lock = asyncio.Lock()
    return lock


@asynccontextmanager
async def ble_connection(address: str, purpose: str) -> AsyncIterator[None]:
    """Hold the shared per-MAC BLE lock for the duration of a connection.

    Emits a WARNING (before awaiting the lock) when the link is already held,
    naming both the waiting ``purpose`` and the current holder, so overlapping
    connect attempts on the same tag are diagnosable.
    """
    key = format_mac(address)
    lock = async_get_ble_lock(address)
    if lock.locked():
        _LOGGER.warning(
            "%s: BLE connection for %s requested while %s holds the device's "
            "single BLE link; waiting for it to finish",
            address,
            purpose,
            _HOLDERS.get(key, "another operation"),
        )
    async with lock:
        _HOLDERS[key] = purpose
        try:
            yield
        finally:
            _HOLDERS.pop(key, None)
