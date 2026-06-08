"""Firmware update entity for OpenDisplay devices."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp
from opendisplay.device import OpenDisplayDevice
from opendisplay.exceptions import BLEConnectionError, OTAError
from opendisplay.models.enums import ICType
from opendisplay.models.firmware import firmware_ota_asset, firmware_release_repo
from opendisplay.ota import perform_silabs_ota

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenDisplayConfigEntry, _get_encryption_key
from .entity import OpenDisplayEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
# GitHub unauthenticated API allows 60 requests/hour; 6h interval = 4 req/day per device
SCAN_INTERVAL = timedelta(hours=6)

_GITHUB_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
_GITHUB_RELEASE = "https://api.github.com/repos/{repo}/releases/tags/{tag}"
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}

# BLE OTA install is only offered for ICs where it completes reliably over an
# ESPHome Bluetooth proxy — the usual HA OS path. EFR32BG22 (Silabs AppLoader)
# does. nRF Legacy DFU does NOT: verified end-to-end, the device receives the
# full, CRC-valid image but the final activate/commit write is unreliable over a
# proxy and strands the device in the bootloader (it works over a *direct*
# connection). So nRF firmware must be flashed directly / via USB-UF2, and OTA
# install is not offered for it here — only release-note visibility.
_OTA_INSTALL_IC_TYPES = {ICType.EFR32BG22}


def _format_firmware_version(major: int, minor: int) -> str:
    """Format firmware version to match GitHub tag convention.

    The 1.x firmware era uses single-digit minor tags (1.0–1.9 on GitHub),
    but the firmware byte may store minor*10 (e.g. minor=60 → "1.6").
    Dividing by 10 aligns the installed string with GitHub tag_names so
    AwesomeVersion comparisons work correctly.
    """
    if major >= 1 and minor >= 10:
        minor = minor // 10
    return f"{major}.{minor}"


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


class OpenDisplayFirmwareUpdateEntity(OpenDisplayEntity[UpdateEntityDescription], UpdateEntity):
    """Firmware update entity for an OpenDisplay device."""

    _attr_latest_version: str | None = None
    should_poll = True  # override coordinator's should_poll=False; GitHub needs regular polling

    def __init__(self, coordinator, entry: OpenDisplayConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, _FIRMWARE_DESCRIPTION)
        fw = entry.runtime_data.firmware
        self._attr_installed_version = _format_firmware_version(fw["major"], fw["minor"])
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
            )
        else:
            self._attr_supported_features = UpdateEntityFeature.RELEASE_NOTES

    @property
    def available(self) -> bool:
        """Stay available while a firmware update is installing.

        During an update the device leaves app mode for the AppLoader, so the
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
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                self._attr_latest_version = data.get("tag_name")
                self._attr_release_notes = data.get("body") or None
        except aiohttp.ClientResponseError as err:
            if err.status in (403, 429):
                _LOGGER.warning("GitHub API rate limited; latest firmware version unchanged")
            else:
                _LOGGER.debug("Failed to fetch latest firmware version: %s", err)
        except aiohttp.ClientError as err:
            _LOGGER.debug("Failed to fetch latest firmware version: %s", err)

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Download and install a firmware update over BLE."""
        tag = version or self._attr_latest_version
        if not tag:
            raise HomeAssistantError("No firmware version available to install")

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

        try:
            firmware_bytes = await self._download_asset(tag, asset_name)

            ble_device = async_ble_device_from_address(
                self.hass, self._ble_address, connectable=True
            )
            if ble_device is None:
                raise HomeAssistantError(
                    "Device not reachable over Bluetooth; bring it within range and retry"
                )

            # Only EFR32BG22 (Silabs AppLoader) is flashed over BLE here — see
            # _OTA_INSTALL_IC_TYPES. The device is either in app mode (and needs the
            # DFU trigger) or already in the AppLoader at the same address (a
            # previous OTA was interrupted, or it browned out before committing).
            # Try to trigger from app mode, but tolerate the connect failing — then
            # the device is already in the AppLoader and we flash it directly, so HA
            # can recover a stuck device instead of failing hard. A stale proxy GATT
            # cache from a prior interrupted OTA is handled at the connection layer:
            # BLEConnection.connect() clears it and retries once.
            try:
                _on_log("Connecting to trigger DFU bootloader…")
                async with OpenDisplayDevice(
                    mac_address=self._ble_address,
                    ble_device=ble_device,
                    encryption_key=_get_encryption_key(self._entry),
                ) as device:
                    # Clear the (now fresh) app-mode GATT from the proxy cache so the
                    # post-reboot AppLoader connection re-discovers the OTA service
                    # instead of these app-firmware handles.
                    cleared = await device.clear_gatt_cache()
                    _on_log(f"Proxy GATT cache clear requested: {cleared}")
                    await device.trigger_dfu_bootloader()
            except BLEConnectionError as err:
                _on_log(
                    f"App-mode connect failed ({err}); device is likely already "
                    "in the AppLoader — attempting OTA directly."
                )

            # AppLoader advertises at the same address; perform_silabs_ota retries
            # the connection internally until it is ready.
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
                on_progress=_on_progress,
                on_log=_on_log,
            )

            self._attr_installed_version = tag
            _LOGGER.info("Firmware updated to %s", tag)

        except (OTAError, BLEConnectionError) as err:
            raise HomeAssistantError(f"Firmware update failed: {err}") from err
        finally:
            self._installing = False
            self._attr_in_progress = False
            self.async_write_ha_state()

    async def _download_asset(self, tag: str, asset_name: str) -> bytes:
        """Fetch the named asset from a GitHub release."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                _GITHUB_RELEASE.format(repo=self._firmware_repo, tag=tag),
                headers=_GITHUB_HEADERS,
            ) as resp:
                resp.raise_for_status()
                release = await resp.json()
        except aiohttp.ClientError as err:
            raise HomeAssistantError(f"Could not fetch release metadata: {err}") from err

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
            async with session.get(download_url) as resp:
                resp.raise_for_status()
                return await resp.read()
        except aiohttp.ClientError as err:
            raise HomeAssistantError(f"Firmware download failed: {err}") from err
