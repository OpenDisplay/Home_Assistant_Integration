"""Unit tests for the process-global per-MAC BLE connection lock.

Exercises the registry keying (case-normalization, distinct MACs), the
contention WARNING, holder bookkeeping, and the ``WeakValueDictionary``
collection that keeps a lock from outliving the loop that bound it. These tests
run on pytest-asyncio's per-test event loop while reusing one ``ADDRESS`` — the
exact shape that would raise ``RuntimeError`` if the registry cached a
loop-bound lock in a plain dict.
"""

import asyncio
import gc
import logging

import pytest

from homeassistant.helpers.device_registry import format_mac

from custom_components.opendisplay.ble_lock import (
    _HOLDERS,
    _LOCKS,
    async_get_ble_lock,
    ble_connection,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
_LOGGER_NAME = "custom_components.opendisplay.ble_lock"


@pytest.mark.asyncio
async def test_case_variant_addresses_share_one_lock():
    """An upper- and lower-cased MAC resolve to the same lock object."""
    upper = async_get_ble_lock("AA:BB:CC:DD:EE:FF")
    lower = async_get_ble_lock("aa:bb:cc:dd:ee:ff")
    assert upper is lower


@pytest.mark.asyncio
async def test_distinct_macs_get_distinct_locks():
    """Different MACs are not serialized against each other."""
    a = async_get_ble_lock("AA:BB:CC:DD:EE:FF")
    b = async_get_ble_lock("11:22:33:44:55:66")
    assert a is not b


@pytest.mark.asyncio
async def test_contended_connection_serializes_and_warns(caplog):
    """A second connect on a held link warns (naming both ops) and waits."""
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    order: list[str] = []
    release_a = asyncio.Event()
    a_holding = asyncio.Event()

    async def op_a() -> None:
        async with ble_connection(ADDRESS, "op-a"):
            order.append("a-enter")
            a_holding.set()
            await release_a.wait()
            order.append("a-exit")

    async def op_b() -> None:
        await a_holding.wait()
        async with ble_connection(ADDRESS, "op-b"):
            order.append("b-enter")

    task_a = asyncio.create_task(op_a())
    await a_holding.wait()
    task_b = asyncio.create_task(op_b())
    # Let B reach (and block on) the contended acquire while A still holds.
    for _ in range(5):
        await asyncio.sleep(0)
    release_a.set()
    await asyncio.gather(task_a, task_b)

    # B's body runs only after A fully exits.
    assert order == ["a-enter", "a-exit", "b-enter"]

    warnings = [
        r for r in caplog.records
        if r.name == _LOGGER_NAME and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert ADDRESS in message  # waiter's address
    assert "op-b" in message   # the waiting purpose
    assert "op-a" in message   # the current holder


@pytest.mark.asyncio
async def test_uncontended_connection_emits_no_warning(caplog):
    """A connect on a free link logs nothing."""
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    async with ble_connection(ADDRESS, "solo"):
        pass

    warnings = [
        r for r in caplog.records
        if r.name == _LOGGER_NAME and r.levelno == logging.WARNING
    ]
    assert warnings == []


@pytest.mark.asyncio
async def test_holder_cleared_after_exit():
    """The holder entry is set while held and popped in the finally."""
    key = format_mac(ADDRESS)
    async with ble_connection(ADDRESS, "op"):
        assert _HOLDERS[key] == "op"
    assert key not in _HOLDERS


@pytest.mark.asyncio
async def test_unreferenced_lock_is_collected():
    """Dropping the last reference lets the WeakValueDictionary evict the lock.

    This is what lets the next test's first acquire mint a lock bound to that
    test's live loop instead of resurrecting a dead-loop one.
    """
    key = format_mac(ADDRESS)
    lock = async_get_ble_lock(ADDRESS)
    assert _LOCKS.get(key) is lock
    del lock
    gc.collect()
    assert key not in _LOCKS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
