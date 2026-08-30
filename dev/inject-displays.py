#!/usr/bin/env python3
"""Write fabricated OpenDisplay config entries into the dev harness's HA storage.

No BLE, no real hardware: this generates N syntactically real ``GlobalConfig``
device configs (py-opendisplay's own model classes, real geometries) and
writes them as config entries carrying ``CONF_CACHED_STATE`` straight into
``dev/ha-config/.storage/core.config_entries``. On the next ``dev/run.sh``,
``__init__.py``'s sleepy-device cache fallback (``_cache_setup_if_sleepy``)
sets each one up without ever attempting a BLE connection: this Docker
container has no Bluetooth adapter, so
``async_ble_device_from_address(..., connectable=True)`` never has a
discovered device to return and comes back ``None`` immediately -- there is
no live-connect attempt to time out or hang on. The fabricated entries force
``options[CONF_SLEEP_MODE] = SLEEP_MODE_ON`` so the sleepy branch is taken
unconditionally, on top of a genuinely battery/deep-sleep ``PowerOption``
that would already resolve the same way under the default "auto" mode.

Usage (uv-run via the repo's own toolchain, matching scripts/*):

    uv run --group dev python dev/inject-displays.py
    uv run --group dev python dev/inject-displays.py --count 5

Run this only while the dev HA container is stopped -- HA must not have its
``.storage`` rewritten out from under a live process (same rule
``dev/restore.sh`` follows). Idempotent: entries whose title carries this
script's marker (see ``TITLE_PREFIX``) are replaced wholesale on every run;
any other integrations' or hand-bootstrapped OpenDisplay entries are left
alone.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from types import MappingProxyType

DEV_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEV_DIR.parent
STORAGE_FILE = DEV_DIR / "ha-config" / ".storage" / "core.config_entries"
COMPOSE_FILE = DEV_DIR / "docker-compose.yml"
COMPOSE_SERVICE = "homeassistant"

# Every fabricated entry's title starts with this, and it is how a re-run
# finds (and replaces) its own previous output without touching anyone
# else's entries -- including a real hardware-bootstrapped OpenDisplay entry,
# which titles itself "OpenDisplay <MAC>" with no such prefix.
TITLE_PREFIX = "OpenDisplay (dev-harness fabricated)"

# Base MAC prefix for fabricated devices -- fake but well-formed BLE
# addresses, never a real OUI. The last octet is the 1-based device index.
FAKE_MAC_PREFIX = "AA:BB:CC:DD:EE"


def _fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _compose_stack_running() -> bool:
    """Return True if the dev HA container is up.

    Best-effort: if docker itself isn't on PATH there is nothing that could
    be holding ha-config/.storage open, so treat that as "not running"
    rather than failing -- the loud, blocking check is for the case that
    actually risks corrupting storage under a live HA process.
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "ps",
                "--status",
                "running",
                "--services",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return COMPOSE_SERVICE in result.stdout.split()


def _build_device_configs():
    """Return (friendly_name, GlobalConfig) pairs for the fabricated fleet.

    Three distinct, realistic panel geometries (small mono / medium BWR /
    large BWRY), each a genuine battery + deep-sleep power config so
    SleepProfile resolves is_sleepy=True even under the default "auto" mode
    -- the forced CONF_SLEEP_MODE=on option (see build_entries) is
    belt-and-suspenders on top of that, not the only thing making it sleepy.
    """
    from opendisplay import (
        BoardManufacturer,
        ColorScheme,
        DataExtended,
        DisplayConfig,
        GlobalConfig,
        ManufacturerData,
        PowerOption,
        SystemConfig,
    )
    from opendisplay.models.config import SeeedBoardType
    from opendisplay.models.enums import PowerMode

    system = SystemConfig(
        ic_type=0,
        communication_modes=0,
        device_flags=0,
        pwr_pin=0xFF,
        reserved=b"\x00" * 15,
    )

    def power(deep_sleep_time_seconds: int) -> PowerOption:
        return PowerOption(
            power_mode=PowerMode.BATTERY,
            battery_capacity_mah=(2000).to_bytes(3, "little"),
            sleep_timeout_ms=10_000,
            tx_power=0,
            sleep_flags=0,
            battery_sense_pin=0xFF,
            battery_sense_enable_pin=0xFF,
            battery_sense_flags=0,
            capacity_estimator=0,
            voltage_scaling_factor=0,
            deep_sleep_current_ua=0,
            deep_sleep_time_seconds=deep_sleep_time_seconds,
            charge_enable_pin=0xFF,
            charge_state_pin=0xFF,
            charger_flags=0,
            min_wake_time_seconds=0,
            screen_timeout_seconds=0,
            reserved=b"\x00" * 4,
        )

    def data_extended(friendly_name: str, model_name: str) -> DataExtended:
        return DataExtended.from_strings(
            manufacturer_name="Seeed Studio",
            model_name=model_name,
            serial_number=f"SN-DEVHARNESS-{friendly_name.upper().replace(' ', '-')}",
            friendly_name=friendly_name,
            device_location="Dev Harness",
            device_id=friendly_name.lower().replace(" ", "-"),
        )

    fleet = [
        (
            "Fabricated Small Mono Tag",
            DisplayConfig(
                instance_number=0,
                display_technology=0,
                panel_ic_type=0,
                pixel_width=250,
                pixel_height=122,
                active_width_mm=49,  # 2.13" panel (Waveshare-class geometry)
                active_height_mm=24,
                tag_type=0,
                rotation=0,
                reset_pin=0xFF,
                busy_pin=0xFF,
                dc_pin=0xFF,
                cs_pin=0xFF,
                data_pin=0,
                partial_update_support=0,
                color_scheme=ColorScheme.MONO.value,
                transmission_modes=0x01,
                clk_pin=0,
                reserved_pins=b"\x00" * 7,
                full_update_mC=0,
                reserved=b"\x00" * 13,
            ),
            SeeedBoardType.EE04,
            900,  # 15 min deep-sleep interval
            '2.13" mono kit',
        ),
        (
            "Fabricated Medium BWR Tag",
            DisplayConfig(
                instance_number=0,
                display_technology=0,
                panel_ic_type=0,
                pixel_width=296,
                pixel_height=128,
                active_width_mm=67,  # 2.9" panel
                active_height_mm=29,
                tag_type=0,
                rotation=0,
                reset_pin=0xFF,
                busy_pin=0xFF,
                dc_pin=0xFF,
                cs_pin=0xFF,
                data_pin=0,
                partial_update_support=0,
                color_scheme=ColorScheme.BWR.value,
                transmission_modes=0x01,
                clk_pin=0,
                reserved_pins=b"\x00" * 7,
                full_update_mC=0,
                reserved=b"\x00" * 13,
            ),
            SeeedBoardType.EN04,
            300,  # 5 min deep-sleep interval
            '2.9" BWR kit',
        ),
        (
            "Fabricated Large BWRY Tag",
            DisplayConfig(
                instance_number=0,
                display_technology=0,
                panel_ic_type=0,
                pixel_width=800,
                pixel_height=480,
                active_width_mm=163,  # 7.3" panel
                active_height_mm=98,
                tag_type=0,
                rotation=0,
                reset_pin=0xFF,
                busy_pin=0xFF,
                dc_pin=0xFF,
                cs_pin=0xFF,
                data_pin=0,
                partial_update_support=0,
                color_scheme=ColorScheme.BWRY.value,
                transmission_modes=0x01,
                clk_pin=0,
                reserved_pins=b"\x00" * 7,
                full_update_mC=0,
                reserved=b"\x00" * 13,
            ),
            SeeedBoardType.OPENDISPLAY_73_COLOR_KIT,
            1800,  # 30 min deep-sleep interval
            '7.3" color kit',
        ),
    ]

    configs = []
    for friendly_name, display, board_type, deep_sleep_s, model_name in fleet:
        config = GlobalConfig(
            system=system,
            manufacturer=ManufacturerData(
                manufacturer_id=BoardManufacturer.SEEED,
                board_type=int(board_type),
                board_revision=0,
                reserved=b"\x00" * 6,
            ),
            power=power(deep_sleep_s),
            displays=[display],
            data_extended=data_extended(friendly_name, model_name),
        )
        configs.append((friendly_name, config))
    return configs


def build_entries(count: int):
    """Return HA config-entry dicts (as_dict()) for `count` fabricated devices."""
    sys.path.insert(0, str(REPO_ROOT))
    from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
    from opendisplay.models.config_json import config_to_json

    from custom_components.opendisplay.const import CONF_CACHED_STATE, CONF_SLEEP_MODE

    fleet = _build_device_configs()
    if count > len(fleet):
        _fail(
            f"--count {count} exceeds the {len(fleet)} distinct fabricated panel "
            "geometries this script knows; add another entry to _build_device_configs "
            "in dev/inject-displays.py, or pass a smaller --count."
        )

    entries = []
    for index in range(count):
        friendly_name, device_config = fleet[index]
        mac = f"{FAKE_MAC_PREFIX}:{index + 1:02X}"
        firmware = {"major": 1, "minor": 0, "patch": 0, "sha": "devharness"}
        cached_state = {
            "config": config_to_json(device_config),
            "firmware": firmware,
            "is_flex": False,
            "landing_url": f"https://opendisplay.org/l/?d=DEVHARNESS{index + 1:02d}",
            "cached_at": 0,
        }
        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain="opendisplay",
            title=f"{TITLE_PREFIX} {friendly_name} ({mac})",
            data={CONF_CACHED_STATE: cached_state},
            source=SOURCE_IMPORT,
            unique_id=mac,
            # SLEEP_MODE_ON is redundant with the battery/deep-sleep power
            # config above (SleepProfile's "auto" mode would already resolve
            # is_sleepy=True from it) but makes the sleepy fallback
            # unconditional, matching the investigation finding this script
            # exists to encode: is_sleepy must come from the fabricated
            # entry, not from ever actually reaching the device.
            options={CONF_SLEEP_MODE: "on"},
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        entries.append(entry.as_dict())
    return entries


def main() -> None:
    """Parse arguments and (re)write the fabricated config entries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--count",
        type=int,
        default=3,
        help="number of fabricated devices to write (default: 3)",
    )
    args = parser.parse_args()
    if args.count < 1:
        _fail("--count must be at least 1")

    if _compose_stack_running():
        _fail(
            "the dev HA stack is running (docker compose -f dev/docker-compose.yml). "
            "Storage must not be rewritten under a live process. Stop it first: "
            "docker compose -f dev/docker-compose.yml down"
        )

    if not STORAGE_FILE.exists():
        _fail(
            f"{STORAGE_FILE} not found. Run dev/run.sh at least once (through "
            "onboarding) first, so HA has created its baseline config_entries store."
        )

    raw = json.loads(STORAGE_FILE.read_text())
    if raw.get("key") != "core.config_entries":
        _fail(
            f"{STORAGE_FILE} has key '{raw.get('key')}', expected 'core.config_entries'. "
            "HA's .storage schema for this file may have changed; script needs updating."
        )

    existing = raw["data"]["entries"]
    kept = [e for e in existing if not e.get("title", "").startswith(TITLE_PREFIX)]
    removed = len(existing) - len(kept)

    fabricated = build_entries(args.count)
    raw["data"]["entries"] = kept + fabricated

    tmp = STORAGE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    tmp.replace(STORAGE_FILE)

    verb = "Replaced" if removed else "Wrote"
    print(
        f"inject-displays: {verb} {removed} previous fabricated entr"
        f"{'y' if removed == 1 else 'ies'} with {len(fabricated)} new one(s) in "
        f"{STORAGE_FILE}:"
    )
    for entry in fabricated:
        print(
            f"  - {entry['title']}  (unique_id={entry['unique_id']}, entry_id={entry['entry_id']})"
        )
    print("\nStart the harness with dev/run.sh -- each entry sets up from its own")
    print("cache (no BLE connection) per __init__.py's sleepy-device fallback.")


if __name__ == "__main__":
    main()
