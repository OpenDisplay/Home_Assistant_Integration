"""Firmware update entity for OpenDisplay devices."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from opendisplay.device import OpenDisplayDevice
from opendisplay.exceptions import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    OTAError,
)
from opendisplay.models.enums import ICType
from opendisplay.models.firmware import firmware_ota_asset, firmware_release_repo
from opendisplay.ota import find_nrf_dfu_device, perform_nrf_dfu, perform_silabs_ota

from . import OpenDisplayConfigEntry, _get_encryption_key
from .ble_lock import ble_connection
from .const import DOMAIN
from .entity import OpenDisplayEntity

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
# GitHub unauthenticated API allows 60 requests/hour; 6h interval = 4 req/day per device
SCAN_INTERVAL = timedelta(hours=6)

# Wall-clock ceiling on the BLE portion of an OTA (DFU trigger + AppLoader flash).
# perform_silabs_ota retries the connection internally, so a device that keeps
# half-connecting could otherwise hold the per-MAC BLE lock indefinitely. Sized
# for a full flash over a slow link; a breach is a genuine failure, not a healthy
# transfer. The firmware download runs outside this (its own aiohttp timeout).
OTA_INSTALL_DEADLINE_S = 600.0
# nRF Legacy DFU is slower: about 5.5 min for a 275 KB image through a proxy
# (paced by its acknowledgements), up to ~12 min paced on a direct adapter.
NRF_OTA_INSTALL_DEADLINE_S = 1200.0

_GITHUB_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
_GITHUB_RELEASE = "https://api.github.com/repos/{repo}/releases/tags/{tag}"
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}

# BLE OTA install is only offered for ICs where it completes reliably over an
# ESPHome Bluetooth proxy — the usual HA OS path. EFR32BG22 (Silabs AppLoader)
# does. nRF52840 Legacy DFU does through py-opendisplay's proxy-safe path (writes
# paced by the proxy's acknowledgements, activate on a fresh long-interval
# connection; nrf-ota docs/esphome-proxy.md). NRF52811 is untested, so it stays
# release-notes only.
_NRF_IC_TYPES = {ICType.NRF52840}
_OTA_INSTALL_IC_TYPES = {ICType.EFR32BG22, *_NRF_IC_TYPES}
# Unpaced transfer on a direct (local adapter) connection. Ignored through a
# proxy, which always uses the paced proxy path.
# TEST BUILD: under evaluation on BlueZ — set False for the 0.05 s paced mode.
_NRF_DIRECT_FAST = True


def _format_firmware_version(major: int, minor: int, patch: int | None = None) -> str:
    """Format firmware version to match GitHub tag convention.

    Firmware parses its own BUILD_VERSION string with a plain int conversion
    (e.g. `atoi` on the substring after the dot), so the minor byte already
    equals the literal digits in the tag_name (1.6 → 6, 1.71 → 71, 2.20 → 20).
    No scaling is needed.

    ``patch`` is None when py-opendisplay (or a cached firmware dict)
    predates the trailing patch byte of the version response; the two-part
    form keeps the old behavior. With patch available, the three-part form
    lets a device on a patch release (e.g. 2.25.1) match its tag instead of
    reporting a phantom pending update forever.
    """
    if patch is None:
        return f"{major}.{minor}"
    return f"{major}.{minor}.{patch}"


_FIRMWARE_DESCRIPTION = UpdateEntityDescription(
    key="firmware",
    translation_key="firmware",
    device_class=UpdateDeviceClass.FIRMWARE,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up OpenDisplay firmware update entity."""
    async_add_entities(
        [OpenDisplayFirmwareUpdateEntity(entry.runtime_data.coordinator, entry)]
    )


class OpenDisplayFirmwareUpdateEntity(
    OpenDisplayEntity[UpdateEntityDescription], UpdateEntity
):
    """Firmware update entity for an OpenDisplay device."""

    _attr_latest_version: str | None = None
    _attr_release_notes: str | None = None
    should_poll = (
        True  # override coordinator's should_poll=False; GitHub needs regular polling
    )

    def __init__(self, coordinator, entry: OpenDisplayConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, _FIRMWARE_DESCRIPTION)
        fw = entry.runtime_data.firmware
        self._attr_installed_version = _format_firmware_version(
            fw["major"], fw["minor"], fw.get("patch")
        )
        ic_type = entry.runtime_data.device_config.system.ic_type
        self._ic_type = ic_type
        self._firmware_repo = firmware_release_repo(ic_type)
        self._ble_address: str = entry.unique_id or ""
        self._entry = entry
        self._installing = False

        if ic_type in _OTA_INSTALL_IC_TYPES:
            self._attr_supported_features = (
                UpdateEntityFeature.INSTALL
                | UpdateEntityFeature.PROGRESS
                | UpdateEntityFeature.RELEASE_NOTES
                | UpdateEntityFeature.SPECIFIC_VERSION
            )
        else:
            self._attr_supported_features = UpdateEntityFeature.RELEASE_NOTES

    @property
    def available(self) -> bool:
        """Stay available while a firmware update is installing.

        During an update the device leaves app mode for its bootloader — an nRF
        device even re-advertises at a different address there — so the
        passive-BLE availability tracker would otherwise mark this entity
        unavailable mid-install and hide the progress, making a working update
        look like a silent failure. The install runs on its own BLE connection
        and is unaffected by the app-mode advertisement stopping, so keep the
        entity available until it finishes.
        """
        return self._installing or super().available

    @property
    def release_url(self) -> str | None:
        """Return URL to the GitHub release page."""
        if self._firmware_repo and self._attr_latest_version:
            return f"https://github.com/{self._firmware_repo}/releases/tag/{self._attr_latest_version}"
        return None

    async def async_release_notes(self) -> str | None:
        """Return the GitHub release body for the latest version."""
        return self._attr_release_notes

    async def async_added_to_hass(self) -> None:
        """Fetch the latest version immediately on entity load."""
        await super().async_added_to_hass()
        await self.async_update()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Fetch latest firmware version from GitHub."""
        if self._firmware_repo is None:
            return
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                _GITHUB_LATEST.format(repo=self._firmware_repo),
                headers=_GITHUB_HEADERS,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                self._attr_latest_version = data.get("tag_name")
                self._attr_release_notes = data.get("body") or None
        except aiohttp.ClientResponseError as err:
            if err.status in (403, 429):
                _LOGGER.warning(
                    "GitHub API rate limited; latest firmware version unchanged"
                )
            else:
                _LOGGER.debug("Failed to fetch latest firmware version: %s", err)
        except aiohttp.ClientError as err:
            _LOGGER.debug("Failed to fetch latest firmware version: %s", err)

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Download and install a firmware update over BLE."""
        tag = version or self._attr_latest_version
        if not tag:
            raise HomeAssistantError("No firmware version available to install")

        # A deep-sleeping tag is dark most of the time and the multi-connection
        # AppLoader flash (DFU trigger -> reconnect -> OTA) cannot be driven
        # reliably inside a ~10 s wake window. Rather than start an install that
        # will strand mid-flash, fail fast with clear guidance to wake it first.
        runtime = self._entry.runtime_data
        profile = runtime.sleep_profile
        if profile.is_sleepy:
            last_seen = (
                runtime.coordinator.data.last_seen if runtime.coordinator.data else None
            )
            if profile.probably_asleep(last_seen):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="device_sleeping_ota",
                )

        asset_name = firmware_ota_asset(self._ic_type, tag)
        if asset_name is None:
            raise HomeAssistantError(
                f"No BLE OTA asset available for IC type {self._ic_type}"
            )

        self._installing = True
        self._attr_in_progress = True
        self.async_write_ha_state()

        last_pct: list[int] = [-1]

        def _on_progress(pct: float) -> None:
            new_pct = int(pct)
            if new_pct != last_pct[0]:
                last_pct[0] = new_pct
                self._attr_in_progress = new_pct
                self.async_write_ha_state()

        def _on_log(msg: str) -> None:
            _LOGGER.debug("OTA: %s", msg)

        # Bound to the OTA deadline block so its .expired() distinguishes a
        # deadline breach from an unrelated TimeoutError (e.g. an aiohttp download
        # total-timeout, which is a bare TimeoutError, not an aiohttp.ClientError).
        ota_deadline: asyncio.Timeout | None = None
        try:
            firmware_bytes = await self._download_asset(tag, asset_name)

            ble_device = async_ble_device_from_address(
                self.hass, self._ble_address, connectable=True
            )
            # An nRF device already in DFU advertises at MAC+1, so its app-mode
            # address resolves to None — expected, and _install_nrf resumes
            # straight to the DFU flash. For other ICs a missing device genuinely
            # means unreachable.
            if ble_device is None and self._ic_type not in _NRF_IC_TYPES:
                raise HomeAssistantError(
                    "Device not reachable over Bluetooth; bring it within range and retry"
                )

            # Hold the per-MAC BLE lock across the whole OTA (DFU trigger +
            # bootloader flash) so a drawcustom/upload/LED/buzzer service call on
            # this tag can't open an overlapping connection mid-flash. This op
            # only takes the lock here — it never calls back into a locked
            # service op — so there is no re-entrant deadlock. The download above
            # runs outside the lock so it doesn't block other BLE ops needlessly.
            # Bound the BLE work with a wall-clock deadline so a device that keeps
            # half-connecting can't hold the lock forever.
            deadline_s = (
                NRF_OTA_INSTALL_DEADLINE_S
                if self._ic_type in _NRF_IC_TYPES
                else OTA_INSTALL_DEADLINE_S
            )
            async with (
                asyncio.timeout(deadline_s) as ota_deadline,
                ble_connection(self._ble_address, "firmware update (OTA)"),
            ):
                if self._ic_type in _NRF_IC_TYPES:
                    await self._install_nrf(
                        firmware_bytes, ble_device, _on_progress, _on_log
                    )
                else:
                    await self._install_silabs(
                        firmware_bytes, ble_device, _on_progress, _on_log
                    )

                self._attr_installed_version = tag
                _LOGGER.info("Firmware updated to %s", tag)

        except TimeoutError as err:
            # Only relabel when our OTA deadline actually fired; re-raise any other
            # TimeoutError (e.g. a download total-timeout) with its true cause.
            if ota_deadline is not None and ota_deadline.expired():
                raise HomeAssistantError(
                    f"Firmware update timed out after {deadline_s:.0f}s"
                ) from err
            raise
        except (AuthenticationFailedError, AuthenticationRequiredError) as err:
            # The app-mode connect for the DFU trigger authenticates with the
            # stored key. An auth failure here is a subclass of OpenDisplayError,
            # not OTAError/BLEConnectionError, so without this it would leak as a
            # raw exception with no reauth. Start reauth and surface the standard
            # auth message.
            _LOGGER.warning(
                "%s: device rejected the encryption key during OTA (%s); "
                "reauthentication required",
                self._ble_address,
                err,
            )
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="authentication_error",
            ) from err
        except ConfigEntryAuthFailed as err:
            # Malformed stored key — already logged at ERROR in _get_encryption_key.
            # Convert to reauth + the standard auth message (a raw ConfigEntryAuthFailed
            # from an entity method does not auto-trigger reauth).
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="authentication_error",
            ) from err
        except (OTAError, BLEConnectionError) as err:
            raise HomeAssistantError(f"Firmware update failed: {err}") from err
        finally:
            self._installing = False
            self._attr_in_progress = False
            self.async_write_ha_state()

    async def _install_nrf(
        self,
        firmware_bytes: bytes,
        ble_device: BLEDevice | None,
        on_progress: Callable[[float], None],
        on_log: Callable[[str], None],
    ) -> None:
        """Flash an nRF52840 through its Nordic Legacy DFU bootloader.

        Triggers DFU from app mode when the device is reachable there. If it is
        already in DFU (no app-mode advertisement, or the app-mode connect fails
        because a previous update was interrupted), skip the trigger and resume
        straight to the flash, so a failed update can simply be retried.
        """
        if ble_device is not None:
            try:
                on_log("Connecting to trigger DFU bootloader…")
                async with OpenDisplayDevice(
                    mac_address=self._ble_address,
                    ble_device=ble_device,
                    encryption_key=_get_encryption_key(self._entry),
                ) as device:
                    await device.trigger_dfu_bootloader()
                on_log("Trigger sent — waiting for the DFU device (up to 30 s)…")
            except BLEConnectionError as err:
                on_log(
                    f"App-mode connect failed ({err}); device is likely already in "
                    "DFU — looking for the DFU device directly."
                )
        else:
            on_log(
                "Device not advertising in app mode — assuming it is already in DFU; "
                "looking for the DFU device directly."
            )

        dfu_device = await find_nrf_dfu_device(self._ble_address)
        if dfu_device is None:
            raise HomeAssistantError(
                "DFU device not found within 30 s. Ensure the device is within BLE "
                "range and try again."
            )
        on_log(f"Found DFU device at {dfu_device.address}")
        await perform_nrf_dfu(
            firmware_bytes,
            dfu_device,
            on_progress=on_progress,
            on_log=on_log,
            fast=_NRF_DIRECT_FAST,
        )

    async def _install_silabs(
        self,
        firmware_bytes: bytes,
        ble_device: BLEDevice | None,
        on_progress: Callable[[float], None],
        on_log: Callable[[str], None],
    ) -> None:
        """Flash an EFR32BG22 through the Silicon Labs AppLoader.

        The device is either in app mode (and needs the DFU trigger) or already
        in the AppLoader at the same address (a previous OTA was interrupted, or
        it browned out before committing). Try to trigger from app mode, but
        tolerate the connect failing — then the device is already in the
        AppLoader and we flash it directly, so HA can recover a stuck device
        instead of failing hard. A stale proxy GATT cache from a prior
        interrupted OTA is handled at the connection layer:
        BLEConnection.connect() clears it and retries.
        """
        try:
            on_log("Connecting to trigger DFU bootloader…")
            async with OpenDisplayDevice(
                mac_address=self._ble_address,
                ble_device=ble_device,
                encryption_key=_get_encryption_key(self._entry),
            ) as device:
                # Clear the (now fresh) app-mode GATT from the proxy cache so the
                # post-reboot AppLoader connection re-discovers the OTA service
                # instead of these app-firmware handles.
                cleared = await device.clear_gatt_cache()
                on_log(f"Proxy GATT cache clear requested: {cleared}")
                await device.trigger_dfu_bootloader()
        except BLEConnectionError as err:
            on_log(
                f"App-mode connect failed ({err}); device is likely already in the "
                "AppLoader — attempting OTA directly."
            )

        # AppLoader advertises at the same address; perform_silabs_ota retries the
        # connection internally until it is ready.
        ota_device = async_ble_device_from_address(
            self.hass, self._ble_address, connectable=True
        )
        if ota_device is None:
            raise HomeAssistantError(
                "Device not reachable in OTA mode; bring it within range and retry"
            )
        await perform_silabs_ota(
            firmware_bytes,
            ota_device,
            on_progress=on_progress,
            on_log=on_log,
        )

    async def _download_asset(self, tag: str, asset_name: str) -> bytes:
        """Fetch the named asset from a GitHub release."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                _GITHUB_RELEASE.format(repo=self._firmware_repo, tag=tag),
                headers=_GITHUB_HEADERS,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                release = await resp.json()
        except aiohttp.ClientError as err:
            raise HomeAssistantError(
                f"Could not fetch release metadata: {err}"
            ) from err

        for asset in release.get("assets", []):
            if asset["name"] == asset_name:
                download_url = asset["browser_download_url"]
                break
        else:
            raise HomeAssistantError(
                f"Asset '{asset_name}' not found in release {tag}; "
                f"available: {[a['name'] for a in release.get('assets', [])]}"
            )

        _LOGGER.debug("Downloading %s from %s", asset_name, download_url)
        try:
            async with session.get(
                download_url, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                resp.raise_for_status()
                return await resp.read()
        except aiohttp.ClientError as err:
            raise HomeAssistantError(f"Firmware download failed: {err}") from err
