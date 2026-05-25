"""Tests for the new BLEDeviceMetadata implementation."""
import os
import sys

# Add metadata paths directly to sys.path to bypass importing custom_components/__init__.py
# which would trigger Home Assistant imports that aren't installed in the test venv.
CURR_DIR = os.path.dirname(os.path.abspath(__file__))
OPENDISPLAY_DIR = os.path.abspath(os.path.join(CURR_DIR, "../custom_components/opendisplay"))

sys.path.insert(0, OPENDISPLAY_DIR)
import metadata as new_metadata_mod
NewBLEDeviceMetadata = new_metadata_mod.BLEDeviceMetadata

sys.path.insert(0, os.path.join(OPENDISPLAY_DIR, "ble"))
import metadata as old_metadata_mod
OldBLEDeviceMetadata = old_metadata_mod.BLEDeviceMetadata


def test_atc_metadata_compatibility():
    """Verify that both old and new BLEDeviceMetadata produce identical results for ATC (flat) devices."""
    raw_metadata = {
        "width": 296,
        "height": 128,
        "model_name": "ATC_2.9",
        "fw_version": 25,
        "rotatebuffer": 1,
        "hw_type": 15,
        "color_scheme": 1,  # BWR
    }

    old_metadata = OldBLEDeviceMetadata(raw_metadata)
    new_metadata = NewBLEDeviceMetadata(raw_metadata)

    # Core properties
    assert new_metadata.width == old_metadata.width == 296
    assert new_metadata.height == old_metadata.height == 128
    assert new_metadata.model_name == old_metadata.model_name == "ATC_2.9"
    assert new_metadata.fw_version == old_metadata.fw_version == 25
    assert new_metadata.formatted_fw_version() == old_metadata.formatted_fw_version() == "0x0019"
    assert new_metadata.rotatebuffer == old_metadata.rotatebuffer == 1
    assert new_metadata.hw_type == old_metadata.hw_type == 15
    assert new_metadata.power_mode == old_metadata.power_mode == 1
    assert new_metadata.is_open_display == old_metadata.is_open_display is False

    # Color scheme properties
    assert new_metadata.color_scheme.value == old_metadata.color_scheme.value == 1
    assert new_metadata.accent_color == old_metadata.accent_color == "red"
    assert new_metadata.is_multi_color == old_metadata.is_multi_color is True

    # Transmission / Upload properties
    assert new_metadata.transmission_modes == old_metadata.transmission_modes == 0
    assert new_metadata.supports_zip_compression == old_metadata.supports_zip_compression is False
    assert new_metadata.get_best_upload_method() == old_metadata.get_best_upload_method() == "block"


def test_opendisplay_metadata_compatibility():
    """Verify that both old and new BLEDeviceMetadata produce identical results for OpenDisplay devices."""
    raw_metadata = {
        "fw_version": "2.0.2",
        "model_name": "OD_7.5_BWR",
        "open_display_config": {
            "version": 1,
            "minor_version": 0,
            "loaded": True,
            "system": {
                "ic_type": 1,
                "communication_modes": 1,
                "device_flags": 0,
                "pwr_pin": 255,
                "reserved": "000000000000000000000000000000",
                "pwr_pin_2": 255,
                "pwr_pin_3": 255,
            },
            "manufacturer": {
                "manufacturer_id": 9286,
                "board_type": 1,
                "board_revision": 0,
                "reserved": "000000000000000000000000000000000000",
            },
            "power": {
                "power_mode": 2,  # USB power
                "battery_capacity_mah": "000000",
                "sleep_timeout_ms": 0,
                "tx_power": 0,
                "sleep_flags": 0,
                "battery_sense_pin": 255,
                "battery_sense_enable_pin": 255,
                "battery_sense_flags": 0,
                "capacity_estimator": 0,
                "voltage_scaling_factor": 0,
                "deep_sleep_current_ua": 0,
                "deep_sleep_time_seconds": 0,
                "reserved": "00000000000000000000",
            },
            "displays": [
                {
                    "instance_number": 0,
                    "display_technology": 0,
                    "panel_ic_type": 0,
                    "pixel_width": 800,
                    "pixel_height": 480,
                    "active_width_mm": 0,
                    "active_height_mm": 0,
                    "open_display_tagtype": 12,
                    "tag_type": 12,
                    "rotation": 90,
                    "reset_pin": 255,
                    "busy_pin": 255,
                    "dc_pin": 255,
                    "cs_pin": 255,
                    "data_pin": 255,
                    "partial_update_support": 1,
                    "color_scheme": 3,  # BWRY
                    "transmission_modes": 10,  # 0x0A (supports ZIP (0x02) and direct_write (0x08))
                    "clk_pin": 255,
                    "reserved_pins": "00000000000000",
                    "full_update_mC": 0,
                    "reserved": "0000000000000000000000000000",
                }
            ],
        }
    }

    old_metadata = OldBLEDeviceMetadata(raw_metadata)
    new_metadata = NewBLEDeviceMetadata(raw_metadata)

    # Core properties
    assert new_metadata.width == old_metadata.width == 800
    assert new_metadata.height == old_metadata.height == 480
    assert new_metadata.model_name == old_metadata.model_name == "OD_7.5_BWR"
    assert new_metadata.fw_version == old_metadata.fw_version == "2.0.2"
    assert new_metadata.formatted_fw_version() == old_metadata.formatted_fw_version() == "2.0.2"
    assert new_metadata.rotatebuffer == old_metadata.rotatebuffer == 90
    assert new_metadata.hw_type == old_metadata.hw_type == 12
    assert new_metadata.power_mode == old_metadata.power_mode == 2
    assert new_metadata.is_open_display == old_metadata.is_open_display is True

    # Color scheme properties
    assert new_metadata.color_scheme.value == old_metadata.color_scheme.value == 3
    assert new_metadata.accent_color == old_metadata.accent_color == "red"
    assert new_metadata.is_multi_color == old_metadata.is_multi_color is True

    # Transmission / Upload properties
    assert new_metadata.transmission_modes == old_metadata.transmission_modes == 10
    assert new_metadata.supports_zip_compression == old_metadata.supports_zip_compression is True
    assert new_metadata.get_best_upload_method() == old_metadata.get_best_upload_method() == "direct_write"
