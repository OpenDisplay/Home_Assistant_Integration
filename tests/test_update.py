"""Test the OpenDisplay firmware update entity."""

from collections.abc import Awaitable, Callable
import time
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.update import (
    ATTR_IN_PROGRESS,
    ATTR_INSTALLED_VERSION,
    ATTR_LATEST_VERSION,
    ATTR_UPDATE_PERCENTAGE,
    DOMAIN as UPDATE_DOMAIN,
    SERVICE_INSTALL,
    UpdateEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from opendisplay import AuthenticationFailedError, BLEConnectionError
from opendisplay.ota import OTAError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.opendisplay.update import _format_firmware_version

from . import (
    DEVICE_CONFIG,
    GITHUB_LATEST,
    OTA_REPO,
    VALID_SERVICE_INFO,
    make_nrf_device_config,
    make_ota_device_config,
)
from .bluetooth import inject_bluetooth_service_info

ENTITY = "update.opendisplay_1234_firmware"
LATEST = "v9.9.9"


@pytest.fixture
def platforms() -> list[Platform]:
    """Only set up the update platform."""
    return [Platform.UPDATE]


@pytest.fixture
def device_config():
    """Default to the IC that supports BLE OTA."""
    return make_ota_device_config()


@pytest.fixture(autouse=True)
def mock_github(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Serve a GitHub release so the entity has a latest version."""
    aioclient_mock.get(
        GITHUB_LATEST, json={"tag_name": LATEST, "body": "release notes here"}
    )
    return aioclient_mock


@pytest.fixture
def setup_seen(
    hass: HomeAssistant, setup_entry: Callable[[], Awaitable[None]]
) -> Callable[[], Awaitable[None]]:
    """Set up the entry and let one advertisement through.

    The entity is unavailable, with no attributes, until the tag is seen once.
    """

    async def _setup() -> None:
        await setup_entry()
        inject_bluetooth_service_info(hass, VALID_SERVICE_INFO)
        await hass.async_block_till_done()

    return _setup


# --- version formatting ----------------------------------------------------
#
# Kept as unit tests: this is pure string handling, and the regression it
# guards (issue #62) was a formatting bug, not a wiring one.


@pytest.mark.parametrize(
    ("major", "minor", "expected"),
    [
        (2, 20, "2.20"),
        (2, 2, "2.2"),
        (1, 0, "1.0"),
        (10, 5, "10.5"),
    ],
)
def test_pre_semver_firmware_reports_two_parts(
    major: int, minor: int, expected: str
) -> None:
    """Firmware older than the SemVer switch reports major.minor only.

    The minor byte holds the literal digits from the tag, so a trailing zero is
    significant: 2.20 is a different release from 2.2, and collapsing them made
    a device on 2.20 show a permanent pending update (issue #62).
    """
    assert _format_firmware_version(major, minor) == expected


@pytest.mark.parametrize(
    ("major", "minor", "patch", "expected"),
    [
        (1, 2, 3, "1.2.3"),
        (2, 25, 1, "2.25.1"),
        (1, 2, 0, "1.2.0"),
    ],
)
def test_semver_firmware_reports_three_parts(
    major: int, minor: int, patch: int, expected: str
) -> None:
    """Newer firmware reports SemVer, including an explicit .0 patch.

    Without the patch component a device on a patch release such as 2.25.1
    would never match its tag and would report a pending update forever.
    """
    assert _format_firmware_version(major, minor, patch) == expected


def test_a_missing_patch_falls_back_to_the_two_part_form() -> None:
    """py-opendisplay, or a cached firmware dict, may predate the patch byte."""
    assert _format_firmware_version(1, 2, None) == "1.2"


# --- entity state ----------------------------------------------------------


async def test_installed_and_latest_version(
    hass: HomeAssistant,
    setup_seen: Callable[[], Awaitable[None]],
) -> None:
    """The entity reports the device's firmware and the newest GitHub release."""
    await setup_seen()

    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_INSTALLED_VERSION] == "1.2.3"
    assert state.attributes[ATTR_LATEST_VERSION] == LATEST
    assert state.attributes["release_url"] == (
        f"https://github.com/{OTA_REPO}/releases/tag/{LATEST}"
    )


async def test_install_is_offered_for_an_ota_capable_ic(
    hass: HomeAssistant,
    setup_seen: Callable[[], Awaitable[None]],
) -> None:
    """An EFR32BG22 supports being flashed over BLE."""
    await setup_seen()

    features = hass.states.get(ENTITY).attributes[ATTR_SUPPORTED_FEATURES]
    assert features & UpdateEntityFeature.INSTALL
    assert features & UpdateEntityFeature.PROGRESS


@pytest.mark.parametrize("device_config", [DEVICE_CONFIG])
async def test_install_is_not_offered_for_other_ics(
    hass: HomeAssistant,
    setup_seen: Callable[[], Awaitable[None]],
) -> None:
    """An IC with no OTA support still shows release notes, but cannot install."""
    await setup_seen()

    features = hass.states.get(ENTITY).attributes[ATTR_SUPPORTED_FEATURES]
    assert not features & UpdateEntityFeature.INSTALL
    assert features & UpdateEntityFeature.RELEASE_NOTES


async def test_a_rate_limited_github_leaves_the_version_unchanged(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_seen: Callable[[], Awaitable[None]],
) -> None:
    """GitHub rate limits are routine and must not break the entity."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(GITHUB_LATEST, status=403)

    await setup_seen()

    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_INSTALLED_VERSION] == "1.2.3"
    assert state.attributes[ATTR_LATEST_VERSION] is None


# --- install ---------------------------------------------------------------


@pytest.fixture
def mock_ota() -> MagicMock:
    """Patch the BLE OTA flash and the asset download."""
    with (
        patch(
            "custom_components.opendisplay.update.perform_silabs_ota",
            AsyncMock(),
        ) as ota,
        patch(
            "custom_components.opendisplay.update.OpenDisplayFirmwareUpdateEntity._download_asset",
            AsyncMock(return_value=b"firmware"),
        ),
    ):
        yield ota


async def _install(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True
    )


async def test_install_flashes_and_records_the_new_version(
    hass: HomeAssistant,
    setup_seen: Callable[[], Awaitable[None]],
    mock_ota: MagicMock,
) -> None:
    """A successful OTA updates the reported installed version."""
    await setup_seen()

    await _install(hass)

    mock_ota.assert_awaited_once()
    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_INSTALLED_VERSION] == LATEST
    assert state.attributes[ATTR_IN_PROGRESS] is False


async def test_install_reports_progress(
    hass: HomeAssistant,
    setup_seen: Callable[[], Awaitable[None]],
    mock_ota: MagicMock,
) -> None:
    """A progress callback mid-flash is visible in the entity state."""
    mid_flash = {}

    async def _flash(firmware, device, on_progress, on_log):
        on_progress(42.0)
        await hass.async_block_till_done()
        mid_flash.update(hass.states.get(ENTITY).attributes)

    mock_ota.side_effect = _flash
    await setup_seen()

    await _install(hass)

    assert mid_flash[ATTR_IN_PROGRESS] is True
    assert mid_flash[ATTR_UPDATE_PERCENTAGE] == 42


@pytest.mark.parametrize(
    "error", [OTAError("flash failed"), BLEConnectionError("link dropped")]
)
async def test_install_failures_surface_as_errors(
    hass: HomeAssistant,
    setup_seen: Callable[[], Awaitable[None]],
    mock_ota: MagicMock,
    error: Exception,
) -> None:
    """A failed flash is reported rather than silently leaving progress stuck."""
    mock_ota.side_effect = error
    await setup_seen()

    with pytest.raises(HomeAssistantError, match="Firmware update failed"):
        await _install(hass)

    # Progress is always cleared, so a retry is possible.
    assert hass.states.get(ENTITY).attributes[ATTR_IN_PROGRESS] is False


async def test_install_auth_failure_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_seen: Callable[[], Awaitable[None]],
    mock_ota: MagicMock,
) -> None:
    """A key rejected during the DFU trigger starts reauth.

    That failure is an OpenDisplayError subclass, not an OTAError, so without
    explicit handling it would escape without ever prompting.
    """
    mock_ota.side_effect = AuthenticationFailedError("bad key")
    await setup_seen()

    with pytest.raises(HomeAssistantError) as err:
        await _install(hass)

    assert err.value.translation_key == "authentication_error"
    flows = hass.config_entries.flow.async_progress_by_handler("opendisplay")
    assert [f for f in flows if f["context"]["source"] == "reauth"]


@pytest.mark.parametrize("device_config", [make_ota_device_config(sleepy=True)])
async def test_install_refused_while_the_device_sleeps(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_seen: Callable[[], Awaitable[None]],
    mock_ota: MagicMock,
) -> None:
    """A deep-sleeping tag is refused rather than stranded mid-flash.

    A sleepy tag stays available far longer than it stays awake, so the gate is
    reached with an advertisement that has merely gone stale.
    """
    await setup_seen()
    runtime = mock_config_entry.runtime_data
    # Seen well over a wake window ago: still available, but back asleep.
    runtime.coordinator.data.last_seen = time.monotonic() - 3600

    with pytest.raises(HomeAssistantError) as err:
        await _install(hass)

    assert err.value.translation_key == "device_sleeping_ota"
    mock_ota.assert_not_awaited()


# --- nRF52840 (Nordic Legacy DFU) -------------------------------------------


NRF_GITHUB_LATEST = "https://api.github.com/repos/OpenDisplay/Firmware/releases/latest"


@pytest.fixture
def mock_nrf_ota(aioclient_mock: AiohttpClientMocker) -> MagicMock:
    """Patch the DFU trigger, DFU-device lookup, nRF flash, and asset download."""
    aioclient_mock.get(NRF_GITHUB_LATEST, json={"tag_name": LATEST, "body": "notes"})
    dfu_device = MagicMock(address="AA:BB:CC:DD:EE:00")
    device = MagicMock()
    device.trigger_dfu_bootloader = AsyncMock()
    opendisplay_device = MagicMock()
    opendisplay_device.return_value.__aenter__ = AsyncMock(return_value=device)
    opendisplay_device.return_value.__aexit__ = AsyncMock(return_value=False)
    with (
        patch(
            "custom_components.opendisplay.update.OpenDisplayDevice",
            opendisplay_device,
        ),
        patch(
            "custom_components.opendisplay.update.find_nrf_dfu_device",
            AsyncMock(return_value=dfu_device),
        ),
        patch(
            "custom_components.opendisplay.update.perform_nrf_dfu", AsyncMock()
        ) as ota,
        patch(
            "custom_components.opendisplay.update.OpenDisplayFirmwareUpdateEntity._download_asset",
            AsyncMock(return_value=b"firmware"),
        ),
    ):
        ota.trigger = device.trigger_dfu_bootloader
        ota.dfu_device = dfu_device
        yield ota


@pytest.mark.parametrize("device_config", [make_nrf_device_config()])
async def test_install_is_offered_for_nrf52840(
    hass: HomeAssistant,
    setup_seen: Callable[[], Awaitable[None]],
    mock_nrf_ota: MagicMock,
) -> None:
    """An nRF52840 can be flashed over BLE, also through a proxy."""
    await setup_seen()

    features = hass.states.get(ENTITY).attributes[ATTR_SUPPORTED_FEATURES]
    assert features & UpdateEntityFeature.INSTALL
    assert features & UpdateEntityFeature.SPECIFIC_VERSION


@pytest.mark.parametrize("device_config", [make_nrf_device_config()])
async def test_nrf_install_triggers_dfu_and_flashes(
    hass: HomeAssistant,
    setup_seen: Callable[[], Awaitable[None]],
    mock_nrf_ota: MagicMock,
) -> None:
    """The app is told to enter DFU, then the bootloader at MAC+1 is flashed."""
    await setup_seen()

    await _install(hass)

    mock_nrf_ota.trigger.assert_awaited_once()
    mock_nrf_ota.assert_awaited_once()
    assert mock_nrf_ota.await_args.args == (b"firmware", mock_nrf_ota.dfu_device)
    assert hass.states.get(ENTITY).attributes[ATTR_INSTALLED_VERSION] == LATEST


@pytest.mark.parametrize("device_config", [make_nrf_device_config()])
async def test_nrf_install_resumes_a_device_already_in_dfu(
    hass: HomeAssistant,
    setup_seen: Callable[[], Awaitable[None]],
    mock_nrf_ota: MagicMock,
) -> None:
    """A failed app-mode connect means it's already in DFU: flash it anyway."""
    await setup_seen()
    mock_nrf_ota.trigger.side_effect = BLEConnectionError("no app")

    await _install(hass)

    mock_nrf_ota.assert_awaited_once()


@pytest.mark.parametrize("device_config", [make_nrf_device_config()])
async def test_nrf_install_fails_when_the_dfu_device_never_appears(
    hass: HomeAssistant,
    setup_seen: Callable[[], Awaitable[None]],
    mock_nrf_ota: MagicMock,
) -> None:
    """No DFU device within the lookup window is a clear error, not a flash."""
    await setup_seen()

    with (
        patch(
            "custom_components.opendisplay.update.find_nrf_dfu_device",
            AsyncMock(return_value=None),
        ),
        pytest.raises(HomeAssistantError, match="DFU device not found"),
    ):
        await _install(hass)

    mock_nrf_ota.assert_not_awaited()


@pytest.mark.parametrize("device_config", [make_nrf_device_config()])
async def test_a_specific_version_can_be_installed(
    hass: HomeAssistant,
    setup_seen: Callable[[], Awaitable[None]],
    mock_nrf_ota: MagicMock,
) -> None:
    """The version passed to the install service is the one flashed."""
    await setup_seen()

    await hass.services.async_call(
        UPDATE_DOMAIN,
        SERVICE_INSTALL,
        {ATTR_ENTITY_ID: ENTITY, "version": "v1.2.4"},
        blocking=True,
    )

    assert hass.states.get(ENTITY).attributes[ATTR_INSTALLED_VERSION] == "v1.2.4"
