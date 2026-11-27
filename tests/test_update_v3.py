"""Test the firmware update entity for displays on firmware 3.x (Firmware_Unified)."""

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.update import (
    ATTR_IN_PROGRESS,
    ATTR_INSTALLED_VERSION,
    ATTR_LATEST_VERSION,
    ATTR_RELEASE_URL,
    ATTR_UPDATE_PERCENTAGE,
    DOMAIN as UPDATE_DOMAIN,
    SERVICE_INSTALL,
    UpdateEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from opendisplay import GlobalConfig, Sec2KeyRequiredError
from opendisplay.firmware import UpdateOutcome, UpdateRefusedError, UpdateResult
from opendisplay.models.enums import ICType
from opendisplay.models.firmware import FirmwareVersion
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.bluetooth import inject_bluetooth_service_info
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from . import DEVICE_CONFIG, GITHUB_LATEST, TEST_ADDRESS, VALID_SERVICE_INFO

ENTITY = "update.opendisplay_1234_firmware"
UNIFIED = "OpenDisplay/Firmware_Unified"
UNIFIED_LATEST = f"https://api.github.com/repos/{UNIFIED}/releases/latest"
TAG = "3.0.3"
UNIFIED_RELEASE = f"https://api.github.com/repos/{UNIFIED}/releases/tags/{TAG}"
DOWNLOAD = f"https://github.com/{UNIFIED}/releases/download/{TAG}/"
IMAGE = b"\x3d\xb8\xf3\x96 an MCUboot image"
OTA_FILE = "opendisplay-xiao_nrf52840-ota.bin"
V3 = FirmwareVersion(major=3, minor=0, patch=2, sha="abc123")
V2 = FirmwareVersion(major=2, minor=3, patch=0, sha="abc123")


def _config(ic_type: int) -> GlobalConfig:
    return replace(DEVICE_CONFIG, system=replace(DEVICE_CONFIG.system, ic_type=ic_type))


def _manifest(image: bytes = IMAGE) -> dict[str, Any]:
    def board(name: str, chip: str, files: list[str]) -> dict[str, Any]:
        return {
            "board": name,
            "target": "nordic-zephyr",
            "chip": chip,
            "flows": {"ble-ota": {"files": files, "protocol": "mcumgr-smp"}},
        }

    return {
        "schema": "opendisplay-release/1",
        "version": TAG,
        "commit": "0" * 40,
        "boards": [
            board("xiao_nrf52840", "nrf52840", [OTA_FILE]),
            board("s3-n16r8", "esp32s3", ["opendisplay-s3-n16r8-app.bin"]),
            board("s3-n8r8", "esp32s3", ["opendisplay-s3-n8r8-app.bin"]),
        ],
        "artefacts": [
            {
                "file": OTA_FILE,
                "board": "xiao_nrf52840",
                "type": "ota",
                "size": len(image),
                "sha256": hashlib.sha256(image).hexdigest(),
            }
        ],
    }


@pytest.fixture
def platforms() -> list[Platform]:
    """Only set up the update platform."""
    return [Platform.UPDATE]


@pytest.fixture
def firmware_version() -> FirmwareVersion:
    """Report firmware 3.0.2."""
    return V3


@pytest.fixture
def device_config() -> GlobalConfig:
    """Use an nRF52840: one board in the release, updated over SMP."""
    return _config(ICType.NRF52840)


@pytest.fixture
def unified_latest() -> str:
    """Return the latest Firmware_Unified tag."""
    return TAG


@pytest.fixture(autouse=True)
def mock_github(
    aioclient_mock: AiohttpClientMocker, unified_latest: str
) -> AiohttpClientMocker:
    """Serve the latest Firmware_Unified release, its manifest and its OTA file."""
    aioclient_mock.get(
        UNIFIED_LATEST, json={"tag_name": unified_latest, "body": "3.x notes"}
    )
    aioclient_mock.get(
        UNIFIED_RELEASE,
        json={
            "tag_name": TAG,
            "assets": [
                {"name": n, "browser_download_url": DOWNLOAD + n}
                for n in ("manifest.json", OTA_FILE)
            ],
        },
    )
    return aioclient_mock


def _serve(aioclient_mock: AiohttpClientMocker, manifest: dict[str, Any]) -> None:
    aioclient_mock.get(DOWNLOAD + "manifest.json", text=json.dumps(manifest))
    aioclient_mock.get(DOWNLOAD + OTA_FILE, content=IMAGE)


@pytest.fixture
def setup_seen(
    hass: HomeAssistant, setup_entry: Callable[[], Awaitable[None]]
) -> Callable[[], Awaitable[None]]:
    """Set up the entry and let one advertisement through."""

    async def _setup() -> None:
        await setup_entry()
        inject_bluetooth_service_info(hass, VALID_SERVICE_INFO)
        await hass.async_block_till_done()

    return _setup


@pytest.fixture
def mock_update() -> Iterator[AsyncMock]:
    """Patch the library's update(); it reports the new version."""
    result = UpdateResult(
        UpdateOutcome.UPDATED, "3.0.2", TAG, f"The display runs {TAG}."
    )
    with patch(
        "custom_components.opendisplay.update.firmware_update",
        AsyncMock(return_value=result),
    ) as update:
        yield update


async def _install(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True
    )


async def test_a_3x_display_follows_firmware_unified(
    hass: HomeAssistant, setup_seen: Callable[[], Awaitable[None]]
) -> None:
    """A 3.x display compares against Firmware_Unified, not its chip's 2.x repo."""
    await setup_seen()

    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_INSTALLED_VERSION] == "3.0.2"
    assert state.attributes[ATTR_LATEST_VERSION] == TAG
    assert state.attributes[ATTR_RELEASE_URL].endswith(f"{UNIFIED}/releases/tag/{TAG}")
    assert state.attributes[ATTR_SUPPORTED_FEATURES] & UpdateEntityFeature.INSTALL
    assert state.state == "on"


@pytest.mark.parametrize("unified_latest", ["4.0.0", "v2.27.1"])
async def test_a_3x_display_is_never_offered_another_major(
    hass: HomeAssistant, setup_seen: Callable[[], Awaitable[None]]
) -> None:
    """A release of another major version is not an update for this display."""
    await setup_seen()

    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_LATEST_VERSION] == "3.0.2"
    assert state.state == "off"


@pytest.mark.parametrize("firmware_version", [V2])
@pytest.mark.parametrize("device_config", [_config(ICType.EFR32BG22)])
async def test_a_2x_display_is_never_offered_3x(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_seen: Callable[[], Awaitable[None]],
) -> None:
    """The move from 2.x to 3.0 needs the user (drive data, claiming), so HA never offers it."""
    aioclient_mock.get(GITHUB_LATEST, json={"tag_name": "v3.0.0", "body": "moved"})
    await setup_seen()

    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_INSTALLED_VERSION] == "2.3.0"
    assert state.attributes[ATTR_LATEST_VERSION] == "2.3.0"
    assert state.state == "off"


@pytest.mark.parametrize(
    "device_config", [_config(ICType.ESP32_S3), _config(ICType.NRF52811)]
)
async def test_a_3x_display_without_a_bluetooth_update_has_no_entity(
    hass: HomeAssistant, setup_seen: Callable[[], Awaitable[None]]
) -> None:
    """ESP32 reports 3.0.0 whatever it runs, and the nRF52811 has no Bluetooth update.

    An entity there would offer an update forever that it cannot install.
    """
    await setup_seen()

    assert hass.states.get(ENTITY) is None


@pytest.mark.parametrize("device_config", [_config(ICType.ESP32_S3)])
async def test_an_entity_left_from_2x_is_removed(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
    setup_seen: Callable[[], Awaitable[None]],
) -> None:
    """The entity a display had on 2.x does not stay behind as unavailable."""
    mock_config_entry.add_to_hass(hass)
    entity_registry.async_get_or_create(
        UPDATE_DOMAIN,
        "opendisplay",
        f"{TEST_ADDRESS}-firmware",
        config_entry=mock_config_entry,
    )

    await setup_seen()

    assert (
        entity_registry.async_get_entity_id(
            UPDATE_DOMAIN, "opendisplay", f"{TEST_ADDRESS}-firmware"
        )
        is None
    )


async def test_install_sends_the_checked_image_and_reloads(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    setup_seen: Callable[[], Awaitable[None]],
    mock_update: AsyncMock,
) -> None:
    """The board's OTA file is sent through update(), then the entry reloads."""
    _serve(aioclient_mock, _manifest())
    await setup_seen()

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await _install(hass)

    mock_update.assert_awaited_once()
    assert mock_update.await_args.args[1] == IMAGE
    reload.assert_called_once_with(mock_config_entry.entry_id)
    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_INSTALLED_VERSION] == TAG
    assert state.attributes[ATTR_IN_PROGRESS] is False


async def test_install_reports_progress(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_seen: Callable[[], Awaitable[None]],
    mock_update: AsyncMock,
) -> None:
    """Bytes sent become the entity's percentage."""
    _serve(aioclient_mock, _manifest())
    mid_flash: dict[str, Any] = {}

    async def _update(device, image, *, on_progress, find_device):
        on_progress(50, 200)
        await hass.async_block_till_done()
        mid_flash.update(hass.states.get(ENTITY).attributes)
        return mock_update.return_value

    mock_update.side_effect = _update
    await setup_seen()

    await _install(hass)

    assert mid_flash[ATTR_UPDATE_PERCENTAGE] == 25


async def test_install_refuses_an_image_that_does_not_match_the_manifest(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_seen: Callable[[], Awaitable[None]],
    mock_update: AsyncMock,
) -> None:
    """A download whose SHA-256 differs from the manifest's is never sent."""
    _serve(aioclient_mock, _manifest(image=b"something else"))
    await setup_seen()

    with pytest.raises(HomeAssistantError) as err:
        await _install(hass)

    assert err.value.translation_key == "firmware_release_unusable"
    mock_update.assert_not_awaited()


@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        (UpdateOutcome.ROLLED_BACK, "The new image did not start"),
        (UpdateOutcome.NOT_UPDATED, "The display keeps running 3.0.2"),
        (UpdateOutcome.WAITING_IN_BOOTLOADER, "send the .gbl again"),
    ],
)
async def test_install_reports_an_update_that_did_not_take(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_seen: Callable[[], Awaitable[None]],
    mock_update: AsyncMock,
    outcome: UpdateOutcome,
    detail: str,
) -> None:
    """Anything but UPDATED is an error carrying the library's explanation."""
    _serve(aioclient_mock, _manifest())
    mock_update.return_value = UpdateResult(outcome, "3.0.2", None, detail)
    await setup_seen()

    with pytest.raises(HomeAssistantError) as err:
        await _install(hass)

    assert err.value.translation_key == "firmware_update_failed"
    assert err.value.translation_placeholders["detail"] == detail
    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_INSTALLED_VERSION] == "3.0.2"
    assert state.attributes[ATTR_IN_PROGRESS] is False


async def test_install_reports_a_refused_update(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_seen: Callable[[], Awaitable[None]],
    mock_update: AsyncMock,
) -> None:
    """The library refusing (claim mode, wrong image) surfaces its reason."""
    _serve(aioclient_mock, _manifest())
    mock_update.side_effect = UpdateRefusedError("The display is in claim mode")
    await setup_seen()

    with pytest.raises(HomeAssistantError) as err:
        await _install(hass)

    assert (
        err.value.translation_placeholders["detail"] == "The display is in claim mode"
    )


async def test_install_with_a_rejected_key_starts_reauth(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_seen: Callable[[], Awaitable[None]],
    mock_update: AsyncMock,
) -> None:
    """A claimed display that refuses the stored key asks for a new one."""
    _serve(aioclient_mock, _manifest())
    mock_update.side_effect = Sec2KeyRequiredError(1)
    await setup_seen()

    with pytest.raises(HomeAssistantError) as err:
        await _install(hass)

    assert err.value.translation_key == "authentication_error"
    flows = hass.config_entries.flow.async_progress_by_handler("opendisplay")
    assert [f for f in flows if f["context"]["source"] == "reauth"]
