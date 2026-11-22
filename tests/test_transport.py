"""Test the WiFi/BLE transport resolver.

The resolver's connection touchpoints (async_ble_device_from_address and
OpenDisplayDevice) are patched in the transport module namespace. Everything
else runs against a really-set-up config entry, so the runtime_data fields the
resolver reads and writes are the real dataclass rather than a stand-in.
"""

from collections.abc import Awaitable, Callable
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from opendisplay import OpenDisplayConnectionError, OpenDisplayTimeoutError
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.opendisplay.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TLS,
    DEFAULT_LAN_PORT,
    MDNS_FRESHNESS_WINDOW_S,
)
from custom_components.opendisplay.transport import (
    TRANSPORT_BLE,
    TRANSPORT_WIFI,
    async_run_with_fallback,
    note_mdns_seen,
    resolve_transport,
)

from . import _async_ctx, ble_unreachable, connects_via

WIFI_DATA = {CONF_HOST: "1.2.3.4", CONF_PORT: 2446, CONF_TLS: False}


@pytest.fixture
def platforms() -> list[Platform]:
    """No platforms; these tests drive the resolver directly."""
    return []


@pytest.fixture
async def entry(
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> MockConfigEntry:
    """Return a loaded config entry, whose runtime_data the resolver uses."""
    await setup_entry()
    return mock_config_entry


def _seen_now(entry: MockConfigEntry) -> None:
    """Mark the tag as just announced over mDNS."""
    entry.runtime_data.mdns_last_seen = time.monotonic()


# -- resolve_transport ------------------------------------------------------


async def test_resolve_no_host_is_ble(entry: MockConfigEntry) -> None:
    """An entry with no CONF_HOST (every pre-WiFi entry) resolves to BLE."""
    resolved = resolve_transport(entry)

    assert resolved.use_wifi is False
    assert resolved.host is None


@pytest.mark.parametrize(
    "config_entry_data", [{CONF_HOST: "1.2.3.4", CONF_PORT: 2447, CONF_TLS: True}]
)
async def test_resolve_host_fresh_mdns_prefers_wifi(entry: MockConfigEntry) -> None:
    """Host present plus a recent mDNS sighting: WiFi, carrying host/port/tls."""
    _seen_now(entry)

    resolved = resolve_transport(entry)

    assert resolved.use_wifi is True
    assert resolved.host == "1.2.3.4"
    assert resolved.port == 2447
    assert resolved.tls is True


@pytest.mark.parametrize("config_entry_data", [{CONF_HOST: "1.2.3.4"}])
async def test_resolve_host_stale_mdns_falls_back_to_ble(
    entry: MockConfigEntry,
) -> None:
    """A host known but not announced within the freshness window: BLE."""
    entry.runtime_data.mdns_last_seen = time.monotonic() - MDNS_FRESHNESS_WINDOW_S - 60

    resolved = resolve_transport(entry)

    assert resolved.use_wifi is False
    # The host is still carried (a caller may log it) but WiFi is not chosen.
    assert resolved.host == "1.2.3.4"
    assert resolved.port == DEFAULT_LAN_PORT


@pytest.mark.parametrize("config_entry_data", [{CONF_HOST: "1.2.3.4"}])
async def test_resolve_host_never_seen_is_ble(entry: MockConfigEntry) -> None:
    """A host with no mDNS sighting yet: BLE, no premature WiFi."""
    assert entry.runtime_data.mdns_last_seen is None

    assert resolve_transport(entry).use_wifi is False


# -- note_mdns_seen ---------------------------------------------------------


@pytest.mark.parametrize("config_entry_data", [{CONF_HOST: "1.2.3.4"}])
async def test_note_mdns_seen_records_timestamp_and_wakes_delivery(
    entry: MockConfigEntry,
) -> None:
    """A sighting stamps the freshness timestamp and triggers a wake."""
    entry.runtime_data.delivery = MagicMock()
    assert entry.runtime_data.mdns_last_seen is None

    note_mdns_seen(entry)

    assert entry.runtime_data.mdns_last_seen is not None
    entry.runtime_data.delivery.notify_device_seen.assert_called_once_with("mdns")


async def test_note_mdns_seen_without_runtime_is_noop() -> None:
    """An unloaded entry (no runtime_data) is tolerated silently."""
    unloaded = MagicMock()
    unloaded.runtime_data = None

    note_mdns_seen(unloaded)  # must not raise


# -- async_run_with_fallback ------------------------------------------------


@pytest.mark.parametrize("config_entry_data", [WIFI_DATA])
async def test_wifi_preferred_when_fresh(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A fresh mDNS host connects over WiFi; BLE is never touched."""
    _seen_now(entry)
    device = MagicMock()
    action = AsyncMock()
    calls: list[dict[str, Any]] = []

    def _factory(**kwargs):
        calls.append(kwargs)
        return _async_ctx(device)

    with connects_via(_factory):
        result = await async_run_with_fallback(
            hass,
            entry,
            action,
            base_kwargs={"config": None},
            ble_unavailable=lambda: AssertionError("BLE must not be reached"),
        )

    assert result == TRANSPORT_WIFI
    action.assert_awaited_once_with(device)
    assert "host" in calls[0]
    assert "mac_address" not in calls[0]
    assert len(calls) == 1  # BLE was never attempted
    assert entry.runtime_data.last_transport == TRANSPORT_WIFI


@pytest.mark.parametrize("config_entry_data", [WIFI_DATA])
@pytest.mark.parametrize(
    "wifi_error",
    [
        OpenDisplayConnectionError("unreachable"),
        OSError("TLS handshake failed"),
        OpenDisplayTimeoutError("read timeout"),
    ],
    ids=["connection-refused", "tls-oserror", "timeout"],
)
async def test_wifi_failure_falls_back_to_ble(
    hass: HomeAssistant, entry: MockConfigEntry, wifi_error: Exception
) -> None:
    """Any WiFi connection failure retries the same action over BLE.

    A raw OSError matters as much as the library's own errors: a TLS handshake
    failure surfaces as a socket error, not an OpenDisplayError.
    """
    _seen_now(entry)
    device = MagicMock()
    action = AsyncMock()

    def _factory(**kwargs):
        if "host" in kwargs:  # the WiFi attempt
            raise wifi_error
        return _async_ctx(device)  # the BLE fallback

    with connects_via(_factory):
        result = await async_run_with_fallback(
            hass,
            entry,
            action,
            base_kwargs={"config": None},
            ble_unavailable=lambda: AssertionError("BLE device was available"),
        )

    assert result == TRANSPORT_BLE
    # The same action ran exactly once, over BLE, after the WiFi attempt raised.
    action.assert_awaited_once_with(device)
    assert entry.runtime_data.last_transport == TRANSPORT_BLE


@pytest.mark.parametrize("config_entry_data", [{CONF_HOST: "1.2.3.4"}])
async def test_stale_host_uses_ble_directly(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """With no fresh mDNS there is no WiFi attempt at all."""
    device = MagicMock()
    action = AsyncMock()
    calls: list[dict[str, Any]] = []

    def _factory(**kwargs):
        calls.append(kwargs)
        return _async_ctx(device)

    with connects_via(_factory):
        result = await async_run_with_fallback(
            hass, entry, action, base_kwargs={}, ble_unavailable=RuntimeError
        )

    assert result == TRANSPORT_BLE
    assert len(calls) == 1
    assert "mac_address" in calls[0]
    assert "host" not in calls[0]


async def test_ble_unavailable_raises_the_supplied_exception(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The caller's exception factory decides what an unreachable tag means.

    Callers use this to raise their own _DeviceUnavailable rather than leaking
    a transport-level error.
    """
    action = AsyncMock()

    class _Sentinel(Exception):
        pass

    with ble_unreachable(), pytest.raises(_Sentinel):
        await async_run_with_fallback(
            hass, entry, action, base_kwargs={}, ble_unavailable=_Sentinel
        )

    action.assert_not_awaited()
