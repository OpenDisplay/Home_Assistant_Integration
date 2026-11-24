"""Test adding drive data to a firmware 3.0 display's config."""

import dataclasses
import json
from pathlib import Path
import stat
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from opendisplay import BLEConnectionError
from opendisplay.panels import MigrationPlan, TargetClass
from opendisplay.setup.configure import ConfigMismatchError
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.opendisplay.repairs import (
    MigrationFailedError,
    MigrationUnavailableError,
    async_migrate,
    async_plan_migration,
)

_MIGRATION = "custom_components.opendisplay.repairs"

PLAN = MigrationPlan(
    needed=True,
    blocked=False,
    reason="adds drive data",
    panel_ic_type=0x0023,
    target_class=TargetClass.DEFAULT,
)
MIGRATED = object()


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Keep backups out of the repository's test config."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


@pytest.fixture
def connected(mock_opendisplay_connect: MagicMock) -> MagicMock:
    """Return the connected device; its config needs drive data until written."""
    device = mock_opendisplay_connect.return_value
    device.config.plan_migration.return_value = PLAN

    async def _written(*_args: object) -> bool:
        device.profile.needs_migration = False
        return False

    device.write_and_verify = AsyncMock(side_effect=_written)
    device.profile.needs_migration = True
    return device


@pytest.fixture
def write_and_verify(connected: MagicMock) -> AsyncMock:
    """Patch the library's write-and-read-back with the connected device's mock."""
    with patch(f"{_MIGRATION}.write_and_verify", connected.write_and_verify):
        yield connected.write_and_verify


async def test_the_plan_comes_from_the_connected_display(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, connected: MagicMock
) -> None:
    """The plan is the library's, for the config the display holds."""
    mock_config_entry.add_to_hass(hass)

    assert await async_plan_migration(hass, mock_config_entry) == PLAN


async def test_an_unreachable_display_cannot_be_planned_for(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_opendisplay_device: MagicMock,
) -> None:
    """A display that does not answer raises the error the flow shows as 'wake it'."""
    mock_config_entry.add_to_hass(hass)
    mock_opendisplay_device.__aenter__.side_effect = BLEConnectionError("gone")

    with pytest.raises(MigrationUnavailableError):
        await async_plan_migration(hass, mock_config_entry)


async def test_migrating_backs_up_the_config_without_secrets_then_writes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    connected: MagicMock,
    write_and_verify: AsyncMock,
) -> None:
    """The old config is saved owner-readable and without secrets before the write."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(f"{_MIGRATION}.apply_migration", return_value=MIGRATED) as apply,
        patch(f"{_MIGRATION}.strip_secrets", side_effect=lambda c: c) as strip,
    ):
        backup = await async_migrate(hass, mock_config_entry)

    assert backup.parent == Path(hass.config.path("opendisplay", "backups"))
    assert json.loads(backup.read_text())["packets"] is not None
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    strip.assert_called_once_with(connected.config.cached)
    apply.assert_called_once_with(connected.config.cached, PLAN)
    write_and_verify.assert_awaited_once_with(connected, MIGRATED)


async def test_a_display_that_still_needs_migration_after_the_write_fails(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    connected: MagicMock,
    write_and_verify: AsyncMock,
) -> None:
    """The write went through but the display still cannot draw: report a failure."""
    mock_config_entry.add_to_hass(hass)
    write_and_verify.side_effect = None
    write_and_verify.return_value = False

    with (
        patch(f"{_MIGRATION}.apply_migration", return_value=MIGRATED),
        pytest.raises(MigrationFailedError),
    ):
        await async_migrate(hass, mock_config_entry)


async def test_a_write_the_display_does_not_read_back_fails(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    connected: MagicMock,
    write_and_verify: AsyncMock,
) -> None:
    """A read-back mismatch keeps the issue; the backup stays on disk."""
    mock_config_entry.add_to_hass(hass)
    write_and_verify.side_effect = ConfigMismatchError("mismatch")

    with (
        patch(f"{_MIGRATION}.apply_migration", return_value=MIGRATED),
        pytest.raises(MigrationFailedError),
    ):
        await async_migrate(hass, mock_config_entry)

    backups = Path(hass.config.path("opendisplay", "backups"))
    assert await hass.async_add_executor_job(lambda: list(backups.iterdir()))


async def test_a_blocked_plan_writes_nothing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    connected: MagicMock,
    write_and_verify: AsyncMock,
) -> None:
    """The plan is checked again at write time; a blocked one is never applied."""
    mock_config_entry.add_to_hass(hass)
    connected.config.plan_migration.return_value = dataclasses.replace(
        PLAN, blocked=True, reason="no display configured"
    )

    with pytest.raises(MigrationFailedError):
        await async_migrate(hass, mock_config_entry)

    write_and_verify.assert_not_awaited()


async def test_two_backups_in_the_same_second_both_survive(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    connected: MagicMock,
    write_and_verify: AsyncMock,
) -> None:
    """A second backup gets its own name; neither is overwritten."""
    mock_config_entry.add_to_hass(hass)

    with patch(f"{_MIGRATION}.apply_migration", return_value=MIGRATED):
        first = await async_migrate(hass, mock_config_entry)
        connected.profile.needs_migration = True
        second = await async_migrate(hass, mock_config_entry)

    assert first != second
    assert first.exists()
    assert second.exists()


async def test_a_backup_that_cannot_be_saved_stops_before_the_write(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    connected: MagicMock,
    write_and_verify: AsyncMock,
    config_dir: Path,
) -> None:
    """Without a backup nothing is written to the display."""
    mock_config_entry.add_to_hass(hass)
    (config_dir / "opendisplay").write_text("not a directory")

    with pytest.raises(MigrationFailedError):
        await async_migrate(hass, mock_config_entry)

    write_and_verify.assert_not_awaited()
