"""Firmware update entity for OpenDisplay devices."""

from __future__ import annotations

import logging

import aiohttp
from opendisplay.models.firmware import firmware_release_repo

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenDisplayConfigEntry
from .entity import OpenDisplayEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

_GITHUB_LATEST = "https://api.github.com/repos/{repo}/releases/latest"

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
    OpenDisplayEntity[UpdateEntityDescription],
    UpdateEntity,
):
    """Firmware update entity for an OpenDisplay device."""

    _attr_latest_version: str | None = None
    should_poll = True

    def __init__(self, coordinator, entry: OpenDisplayConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, _FIRMWARE_DESCRIPTION)
        fw = entry.runtime_data.firmware
        self._attr_installed_version = f"{fw['major']}.{fw['minor']}"
        self._firmware_repo = firmware_release_repo(
            entry.runtime_data.device_config.system.ic_type
        )

    @property
    def installed_version(self) -> str | None:
        """Return the installed firmware version."""
        return self._attr_installed_version

    @property
    def latest_version(self) -> str | None:
        """Return the latest available firmware version."""
        return self._attr_latest_version

    @property
    def release_url(self) -> str | None:
        """Return URL to the GitHub release page."""
        if self._firmware_repo and self._attr_latest_version:
            return (
                f"https://github.com/{self._firmware_repo}/releases/tag/"
                f"{self._attr_latest_version}"
            )
        return None

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
                headers={"Accept": "application/vnd.github+json"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                self._attr_latest_version = data.get("tag_name")
        except aiohttp.ClientError as err:
            _LOGGER.debug("Failed to fetch latest firmware version: %s", err)
