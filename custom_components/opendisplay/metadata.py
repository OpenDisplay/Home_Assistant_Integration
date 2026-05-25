"""New BLE Device Metadata Abstraction using py-opendisplay.

Provides a clean interface for accessing device metadata that transparently
handles differences between ATC (flat structure) and OpenDisplay (nested config) formats,
utilizing py-opendisplay library configuration models.
"""
from __future__ import annotations

from typing import Any

from opendisplay import ColorScheme as ODColorScheme
from opendisplay.models.config import (
    GlobalConfig,
    SystemConfig,
    ManufacturerData,
    PowerOption,
    DisplayConfig,
    LedConfig,
    SensorData,
    DataBus,
    BinaryInputs,
    WifiConfig,
    SecurityConfig,
    TouchController,
    PassiveBuzzer,
)

def _dict_to_global_config(data: dict[str, Any]) -> GlobalConfig:
    """Reconstruct a GlobalConfig dataclass from its nested dict representation."""
    def _to_bytes(val: Any) -> bytes:
        if isinstance(val, str):
            return bytes.fromhex(val)
        return val

    # Extract single instances
    sys_data = data.get("system") or {}
    system = SystemConfig(
        ic_type=sys_data.get("ic_type", 0),
        communication_modes=sys_data.get("communication_modes", 0),
        device_flags=sys_data.get("device_flags", 0),
        pwr_pin=sys_data.get("pwr_pin", 0xFF),
        reserved=_to_bytes(sys_data.get("reserved", b"\x00" * 15)),
        pwr_pin_2=sys_data.get("pwr_pin_2", 0xFF),
        pwr_pin_3=sys_data.get("pwr_pin_3", 0xFF),
    )

    mfr_data = data.get("manufacturer") or {}
    manufacturer = ManufacturerData(
        manufacturer_id=mfr_data.get("manufacturer_id", 0x2446),
        board_type=mfr_data.get("board_type", 0),
        board_revision=mfr_data.get("board_revision", 0),
        reserved=_to_bytes(mfr_data.get("reserved", b"\x00" * 18)),
    )

    pwr_data = data.get("power") or {}
    power = PowerOption(
        power_mode=pwr_data.get("power_mode", 1),
        battery_capacity_mah=_to_bytes(pwr_data.get("battery_capacity_mah", b"\x00" * 3)),
        sleep_timeout_ms=pwr_data.get("sleep_timeout_ms", 0),
        tx_power=pwr_data.get("tx_power", 0),
        sleep_flags=pwr_data.get("sleep_flags", 0),
        battery_sense_pin=pwr_data.get("battery_sense_pin", 0xFF),
        battery_sense_enable_pin=pwr_data.get("battery_sense_enable_pin", 0xFF),
        battery_sense_flags=pwr_data.get("battery_sense_flags", 0),
        capacity_estimator=pwr_data.get("capacity_estimator", 0),
        voltage_scaling_factor=pwr_data.get("voltage_scaling_factor", 0),
        deep_sleep_current_ua=pwr_data.get("deep_sleep_current_ua", 0),
        deep_sleep_time_seconds=pwr_data.get("deep_sleep_time_seconds", 0),
        reserved=_to_bytes(pwr_data.get("reserved", b"\x00" * 10)),
    )

    # Extract displays
    displays = []
    for d in data.get("displays", []):
        displays.append(
            DisplayConfig(
                instance_number=d.get("instance_number", 0),
                display_technology=d.get("display_technology", 0),
                panel_ic_type=d.get("panel_ic_type", 0),
                pixel_width=d.get("pixel_width", 0),
                pixel_height=d.get("pixel_height", 0),
                active_width_mm=d.get("active_width_mm", 0),
                active_height_mm=d.get("active_height_mm", 0),
                tag_type=d.get("open_display_tagtype") or d.get("tag_type") or 0,
                rotation=d.get("rotation", 0),
                reset_pin=d.get("reset_pin", 0xFF),
                busy_pin=d.get("busy_pin", 0xFF),
                dc_pin=d.get("dc_pin", 0xFF),
                cs_pin=d.get("cs_pin", 0xFF),
                data_pin=d.get("data_pin", 0xFF),
                partial_update_support=d.get("partial_update_support", 0),
                color_scheme=d.get("color_scheme", 0),
                transmission_modes=d.get("transmission_modes", 0),
                clk_pin=d.get("clk_pin", 0xFF),
                reserved_pins=_to_bytes(d.get("reserved_pins", b"\x00" * 7)),
                full_update_mC=d.get("full_update_mC", 0),
                reserved=_to_bytes(d.get("reserved", b"\x00" * 13)),
            )
        )

    # Extract leds
    leds = []
    for l in data.get("leds", []):
        leds.append(
            LedConfig(
                instance_number=l.get("instance_number", 0),
                led_type=l.get("led_type", 0),
                led_1_r=l.get("led_1_r", 0),
                led_2_g=l.get("led_2_g", 0),
                led_3_b=l.get("led_3_b", 0),
                led_4=l.get("led_4", 0),
                led_flags=l.get("led_flags", 0),
                reserved=_to_bytes(l.get("reserved", b"\x00" * 15)),
            )
        )

    # Extract sensors
    sensors = []
    for s in data.get("sensors", []):
        sensors.append(
            SensorData(
                instance_number=s.get("instance_number", 0),
                sensor_type=s.get("sensor_type", 0),
                bus_id=s.get("bus_id", 0),
                i2c_addr_7bit=s.get("i2c_addr_7bit", 0),
                msd_data_start_byte=s.get("msd_data_start_byte", 0),
                reserved=_to_bytes(s.get("reserved", b"\x00" * 24)),
            )
        )

    # Extract data buses
    data_buses = []
    buses_list = data.get("data_buses") or data.get("buses") or []
    for b in buses_list:
        data_buses.append(
            DataBus(
                instance_number=b.get("instance_number", 0),
                bus_type=b.get("bus_type", 0),
                pin_1=b.get("pin_1", 0xFF),
                pin_2=b.get("pin_2", 0xFF),
                pin_3=b.get("pin_3", 0xFF),
                pin_4=b.get("pin_4", 0xFF),
                pin_5=b.get("pin_5", 0xFF),
                pin_6=b.get("pin_6", 0xFF),
                pin_7=b.get("pin_7", 0xFF),
                bus_speed_hz=b.get("bus_speed_hz", 0),
                bus_flags=b.get("bus_flags", 0),
                pullups=b.get("pullups", 0),
                pulldowns=b.get("pulldowns", 0),
                reserved=_to_bytes(b.get("reserved", b"\x00" * 14)),
            )
        )

    # Extract binary inputs
    binary_inputs = []
    inputs_list = data.get("binary_inputs") or data.get("inputs") or []
    for bi in inputs_list:
        binary_inputs.append(
            BinaryInputs(
                instance_number=bi.get("instance_number", 0),
                input_type=bi.get("input_type", 0),
                display_as=bi.get("display_as", 0),
                reserved_pins=_to_bytes(bi.get("reserved_pins", b"\x00" * 8)),
                input_flags=bi.get("input_flags", 0),
                invert=bi.get("invert", 0),
                pullups=bi.get("pullups", 0),
                pulldowns=bi.get("pulldowns", 0),
                button_data_byte_index=bi.get("button_data_byte_index", 0),
                reserved=_to_bytes(bi.get("reserved", b"\x00" * 14)),
            )
        )

    # Extract optional configs
    wifi_config = None
    if "wifi_config" in data and data["wifi_config"] is not None:
        w = data["wifi_config"]
        wifi_config = WifiConfig(
            ssid=_to_bytes(w.get("ssid", b"\x00" * 32)),
            password=_to_bytes(w.get("password", b"\x00" * 32)),
            encryption_type=w.get("encryption_type", 0),
            server_url=_to_bytes(w.get("server_url", b"\x00" * 64)),
            server_port=w.get("server_port", 2446),
            reserved=_to_bytes(w.get("reserved", b"\x00" * 29)),
        )

    security_config = None
    if "security_config" in data and data["security_config"] is not None:
        sec = data["security_config"]
        security_config = SecurityConfig(
            encryption_enabled=sec.get("encryption_enabled", 0),
            encryption_key=_to_bytes(sec.get("encryption_key", b"\x00" * 16)),
            session_timeout_seconds=sec.get("session_timeout_seconds", 0),
            flags=sec.get("flags", 0),
            reset_pin=sec.get("reset_pin", 0xFF),
            reserved=_to_bytes(sec.get("reserved", b"\x00" * 43)),
        )

    touch_controllers = []
    for tc in data.get("touch_controllers", []):
        touch_controllers.append(
            TouchController(
                instance_number=tc.get("instance_number", 0),
                touch_ic_type=tc.get("touch_ic_type", 0),
                bus_id=tc.get("bus_id", 0xFF),
                i2c_addr_7bit=tc.get("i2c_addr_7bit", 0),
                int_pin=tc.get("int_pin", 0xFF),
                rst_pin=tc.get("rst_pin", 0xFF),
                display_instance=tc.get("display_instance", 0),
                flags=tc.get("flags", 0),
                poll_interval_ms=tc.get("poll_interval_ms", 0),
                touch_data_start_byte=tc.get("touch_data_start_byte", 0),
                reserved=_to_bytes(tc.get("reserved", b"\x00" * 21)),
            )
        )

    buzzers = []
    for bz in data.get("buzzers", []):
        buzzers.append(
            PassiveBuzzer(
                instance_number=bz.get("instance_number", 0),
                drive_pin=bz.get("drive_pin", 0),
                enable_pin=bz.get("enable_pin", 0xFF),
                flags=bz.get("flags", 0),
                duty_percent=bz.get("duty_percent", 0),
                reserved=_to_bytes(bz.get("reserved", b"\x00" * 27)),
            )
        )

    return GlobalConfig(
        system=system,
        manufacturer=manufacturer,
        power=power,
        displays=displays,
        leds=leds,
        sensors=sensors,
        data_buses=data_buses,
        binary_inputs=binary_inputs,
        wifi_config=wifi_config,
        security_config=security_config,
        touch_controllers=touch_controllers,
        buzzers=buzzers,
        version=data.get("version", 0),
        minor_version=data.get("minor_version", 0),
        loaded=data.get("loaded", False),
    )


class BLEDeviceMetadata:
    """Abstraction for BLE device metadata.

    Wraps raw metadata dictionary and provides clean property-based access
    to device capabilities, using py-opendisplay configuration models.

    Args:
        raw_metadata: Dictionary containing device metadata
    """

    def __init__(self, raw_metadata: dict[str, Any]) -> None:
        """Initialize BLE device metadata wrapper.

        Args:
            raw_metadata: Device metadata dictionary from config entry
        """
        if "open_display_config" not in raw_metadata and "oepl_config" in raw_metadata:
            self._metadata = {**raw_metadata, "open_display_config": raw_metadata["oepl_config"]}
        else:
            self._metadata = raw_metadata
        self._is_open_display = "open_display_config" in self._metadata
        
        self._config: GlobalConfig | None = None
        if self._is_open_display:
            try:
                self._config = _dict_to_global_config(self._metadata["open_display_config"])
            except Exception:
                pass

    @property
    def width(self) -> int:
        """Get display width in pixels.

        Returns:
            Display width, or 0 if not available
        """
        if self._is_open_display and self._config and self._config.displays:
            return self._config.displays[0].pixel_width
        return self._metadata.get("width", 0)

    @property
    def height(self) -> int:
        """Get display height in pixels.

        Returns:
            Display height, or 0 if not available
        """
        if self._is_open_display and self._config and self._config.displays:
            return self._config.displays[0].pixel_height
        return self._metadata.get("height", 0)

    @property
    def model_name(self) -> str:
        """Get device model name.

        Returns:
            Model name string, or "Unknown" if not available
        """
        return self._metadata.get("model_name", "Unknown")

    @property
    def fw_version(self) -> int | str:
        """Get firmware version.

        Returns:
            Firmware version number or string, or 0/"" if not available
        """
        if self._is_open_display:
            # Prefer explicit string/parsed version saved from interrogation
            if "fw_version" in self._metadata:
                return self._metadata.get("fw_version", "")
            major = self._metadata.get("fw_version_major")
            minor = self._metadata.get("fw_version_minor")
            if major is not None and minor is not None:
                return f"{major}.{minor}"
        return self._metadata.get("fw_version", 0)

    def formatted_fw_version(self) -> str | None:
        """Return firmware version formatted for display."""
        fw = self.fw_version
        if fw in (None, ""):
            return None
        if isinstance(fw, int):
            return f"0x{fw:04x}"
        return str(fw)

    @property
    def rotatebuffer(self) -> int:
        """Get rotation setting.

        For OpenDisplay devices, returns the rotation value from display config.
        For ATC devices, returns the rotatebuffer flag.

        Returns:
            Rotation value (0, 1, 2, or 3) or rotatebuffer flag (0 or 1)
        """
        if self._is_open_display and self._config and self._config.displays:
            return self._config.displays[0].rotation
        return self._metadata.get("rotatebuffer", 0)

    @property
    def hw_type(self) -> int:
        """Get hardware type identifier.

        Returns:
            Hardware type code, or 0 if not available
        """
        if self._is_open_display and self._config and self._config.displays:
            return self._config.displays[0].tag_type
        return self._metadata.get("hw_type", 0)

    @property
    def power_mode(self) -> int:
        """Get power mode setting.

        Returns:
            Power mode: 1=battery, 2=USB, 3=solar
            ATC devices always return 1 (battery)
        """
        if self._is_open_display and self._config and self._config.power:
            return self._config.power.power_mode
        return 1  # ATC devices always have batteries

    @property
    def is_open_display(self) -> bool:
        """Check if this is an OpenDisplay device.

        Returns:
            True if OpenDisplay device, False if ATC device
        """
        return self._is_open_display

    @property
    def color_scheme(self) -> ODColorScheme:
        """Get ColorScheme enum for this device."""
        if self._is_open_display and self._config and self._config.displays:
            raw_scheme = self._config.displays[0].color_scheme
        else:
            raw_scheme = self._metadata.get("color_scheme", 0)
        return ODColorScheme.from_value(raw_scheme)

    @property
    def accent_color(self) -> str:
        """Get accent color name.

        Returns:
            Accent color name from color scheme palette
        """
        return self.color_scheme.accent_color

    @property
    def is_multi_color(self) -> bool:
        """Check if device supports multiple colors.

        Returns:
            True if color scheme has more than 2 colors, False otherwise
        """
        return len(self.color_scheme.palette.colors) > 2

    @property
    def transmission_modes(self) -> int:
        """Get supported transmission modes (bitfield).

        Bit flags:
        - Bit 0 (0x01): raw transfer (block-based uncompressed)
        - Bit 1 (0x02): zip compressed transfer (block-based compressed)
        - Bit 3 (0x08): direct_write mode

        Returns:
            Transmission modes bitfield, or 0 if not available
            ATC devices return 0 (assume block-based only for backward compatibility)
        """
        if self._is_open_display and self._config and self._config.displays:
            return self._config.displays[0].transmission_modes
        return 0  # ATC devices don't support direct_write

    @property
    def supports_zip_compression(self) -> bool:
        """Return true if the device advertises zip-compressed transfer support."""
        return (self.transmission_modes & 0x02) != 0

    def get_best_upload_method(self) -> str:
        """Determine the best upload method based on device capabilities.

        Priority order:
        1. direct_write: If direct_write (0x08) is supported
        2. block: Fallback to block-based upload (always supported)

        Returns:
            Upload method string: "direct_write" or "block"
        """
        modes = self.transmission_modes
        has_direct_write = (modes & 0x08) != 0

        if has_direct_write:
            return "direct_write"
        else:
            return "block"
