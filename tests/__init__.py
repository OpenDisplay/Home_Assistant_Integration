"""Tests for the OpenDisplay integration."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from ipaddress import ip_address
from time import time
from unittest.mock import MagicMock, patch

from bleak.backends.scanner import AdvertisementData
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from opendisplay import (
    BinaryInputs,
    BoardManufacturer,
    ColorScheme,
    DataExtended,
    DisplayConfig,
    GlobalConfig,
    ManufacturerData,
    PowerOption,
    SecurityConfig,
    SystemConfig,
    WifiConfig,
)
from opendisplay.models.config import (
    LedConfig,
    NfcConfig,
    PassiveBuzzer,
    SensorData,
    TouchController,
)
from opendisplay.models.config_json import config_to_json
from opendisplay.models.enums import ICType, PowerMode, SensorType

from tests.bluetooth import generate_ble_device

OPENDISPLAY_MANUFACTURER_ID = 9286  # 0x2446

# V1 advertisement payload (14 bytes):
# battery_mv=3700, temperature_c=25.0, loop_counter=1
V1_ADVERTISEMENT_DATA = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x82\x72\x11"

TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"
TEST_TITLE = "OpenDisplay 1234"
# Firmware advertises as "OD" followed by the device id in hex.
TEST_NAME = "OD1A2B3C"
ENCRYPTION_KEY = "aabbccddee112233aabbccddee112233"  # 32 hex chars = 16 bytes

# The device registry's configuration_url, built by landing_url().
LANDING_URL = "https://opendisplay.org/l/?d=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

# Firmware version response: major=1, minor=2, patch=3, sha="abc123"
FIRMWARE_VERSION = {"major": 1, "minor": 2, "patch": 3, "sha": "abc123"}

DEVICE_CONFIG = GlobalConfig(
    system=SystemConfig(
        ic_type=0,
        communication_modes=0,
        device_flags=0,
        pwr_pin=0xFF,
        reserved=b"\x00" * 17,
    ),
    manufacturer=ManufacturerData(
        manufacturer_id=BoardManufacturer.SEEED,
        board_type=1,
        board_revision=0,
        reserved=b"\x00" * 6,
    ),
    power=PowerOption(
        power_mode=0,
        battery_capacity_mah=b"\x00" * 3,
        sleep_timeout_ms=0,
        tx_power=0,
        sleep_flags=0,
        battery_sense_pin=0xFF,
        battery_sense_enable_pin=0xFF,
        battery_sense_flags=0,
        capacity_estimator=0,
        voltage_scaling_factor=0,
        deep_sleep_current_ua=0,
        deep_sleep_time_seconds=0,
        charge_enable_pin=0xFF,
        charge_state_pin=0xFF,
        charger_flags=0,
        min_wake_time_seconds=0,
        screen_timeout_seconds=0,
        reserved=b"\x00" * 4,
    ),
    displays=[
        DisplayConfig(
            instance_number=0,
            display_technology=0,
            panel_ic_type=0,
            pixel_width=296,
            pixel_height=128,
            active_width_mm=67,
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
            reserved=b"\x00" * 33,
        )
    ],
    security_config=SecurityConfig(
        encryption_enabled=1,
        encryption_key=bytes.fromhex(ENCRYPTION_KEY),
        session_timeout_seconds=0,
        flags=0,
        reset_pin=0xFF,
        reserved=b"\x00" * 43,
    ),
    data_extended=DataExtended.from_strings(
        manufacturer_name="Seeed Studio",
        model_name="XIAO EN04(NRF52840)",
        serial_number="SN-0123456789",
        friendly_name="Living Room Tag",
        device_location="Living Room",
        device_id="device-1234",
        custom_string_1="custom one",
        custom_string_2="custom two",
        custom_string_3="custom three",
    ),
)


def make_service_info(
    name: str | None = TEST_NAME,
    address: str = "AA:BB:CC:DD:EE:FF",
    manufacturer_data: dict[int, bytes] | None = None,
) -> BluetoothServiceInfoBleak:
    """Create a BluetoothServiceInfoBleak for testing."""
    if manufacturer_data is None:
        manufacturer_data = {OPENDISPLAY_MANUFACTURER_ID: V1_ADVERTISEMENT_DATA}
    return BluetoothServiceInfoBleak(
        name=name or "",
        address=address,
        rssi=-60,
        manufacturer_data=manufacturer_data,
        service_data={},
        service_uuids=[],
        source="local",
        connectable=True,
        time=time(),
        device=generate_ble_device(address, name=name),
        advertisement=AdvertisementData(
            local_name=name,
            manufacturer_data=manufacturer_data,
            service_data={},
            service_uuids=[],
            rssi=-60,
            tx_power=-127,
            platform_data=(),
        ),
        tx_power=-127,
    )


BINARY_INPUT = BinaryInputs(
    instance_number=0,
    input_type=0,
    display_as=0,
    reserved_pins=b"\x00" * 8,
    input_flags=0x01,  # bit 0 set → button_id 0 active
    invert=0,
    pullups=0,
    pulldowns=0,
    button_data_byte_index=0,
)

BUTTON_DEVICE_CONFIG = GlobalConfig(
    system=DEVICE_CONFIG.system,
    manufacturer=DEVICE_CONFIG.manufacturer,
    power=DEVICE_CONFIG.power,
    displays=DEVICE_CONFIG.displays,
    binary_inputs=[BINARY_INPUT],
)


def make_v1_service_info(
    dynamic_data: bytes = b"\x00" * 11,
    name: str | None = TEST_NAME,
    address: str = TEST_ADDRESS,
    reboot: bool = False,
    loop_counter: int = 0x11,
) -> BluetoothServiceInfoBleak:
    """Create a v1 advertisement service info with a custom 11-byte dynamic block.

    ``reboot`` sets bit 1 of the final byte, which the firmware raises on boot
    and clears on the first BLE connection.
    """
    # temperature=25.0°C, battery≈3700 mV
    flags = loop_counter | (0x02 if reboot else 0x00)
    return make_service_info(
        name=name,
        address=address,
        manufacturer_data={
            OPENDISPLAY_MANUFACTURER_ID: dynamic_data + b"\x82\x72" + bytes([flags])
        },
    )


def make_binary_inputs(
    instance_number: int = 0,
    byte_index: int = 0,
    input_flags: int = 0x01,
) -> BinaryInputs:
    """Create a minimal BinaryInputs config entry.

    input_flags is a bitmask of active inputs: bit N set means button_id N is active.
    """
    return BinaryInputs(
        instance_number=instance_number,
        input_type=0,
        display_as=0,
        reserved_pins=b"\x00" * 8,
        input_flags=input_flags,
        invert=0,
        pullups=0,
        pulldowns=0,
        button_data_byte_index=byte_index,
    )


SHT40_READING = bytes.fromhex("7ca20a")  # 28.0 C / 63.6 %RH
SHT40_START_BYTE = 7


def make_sht40_device_config(
    msd_data_start_byte: int = SHT40_START_BYTE,
) -> GlobalConfig:
    """Return a GlobalConfig carrying one SHT40 ambient sensor."""
    return GlobalConfig(
        system=DEVICE_CONFIG.system,
        manufacturer=DEVICE_CONFIG.manufacturer,
        power=DEVICE_CONFIG.power,
        displays=DEVICE_CONFIG.displays,
        sensors=[
            SensorData(
                instance_number=0,
                sensor_type=SensorType.SHT40,
                bus_id=0,
                i2c_addr_7bit=0x44,
                msd_data_start_byte=msd_data_start_byte,
            )
        ],
    )


def make_sht40_service_info(
    block: bytes = SHT40_READING, start_byte: int = SHT40_START_BYTE
) -> BluetoothServiceInfoBleak:
    """Return a v1 advertisement whose dynamic block carries an SHT40 reading."""
    dynamic = bytearray(11)
    dynamic[start_byte : start_byte + 3] = block
    return make_v1_service_info(dynamic_data=bytes(dynamic))


def make_sleepy_device_config() -> GlobalConfig:
    """Return a GlobalConfig for a battery tag that deep-sleeps between wakes."""
    return GlobalConfig(
        system=DEVICE_CONFIG.system,
        manufacturer=DEVICE_CONFIG.manufacturer,
        power=replace(
            DEVICE_CONFIG.power,
            power_mode=PowerMode.BATTERY,
            deep_sleep_time_seconds=300,
            sleep_timeout_ms=10_000,
        ),
        displays=DEVICE_CONFIG.displays,
    )


def make_cached_state(config: GlobalConfig | None = None) -> dict:
    """Return the CONF_CACHED_STATE payload a previous interrogation would store."""
    return {
        "config": config_to_json(config if config is not None else DEVICE_CONFIG),
        "firmware": FIRMWARE_VERSION,
        "is_flex": True,
        "landing_url": LANDING_URL,
    }


TOUCH_START_BYTE = 0


def make_touch_device_config(
    touch_data_start_byte: int = TOUCH_START_BYTE,
) -> GlobalConfig:
    """Return a GlobalConfig carrying one touch controller."""
    return GlobalConfig(
        system=DEVICE_CONFIG.system,
        manufacturer=DEVICE_CONFIG.manufacturer,
        power=DEVICE_CONFIG.power,
        displays=DEVICE_CONFIG.displays,
        touch_controllers=[
            TouchController(
                instance_number=0,
                touch_ic_type=0,
                bus_id=0,
                i2c_addr_7bit=0x15,
                int_pin=0xFF,
                rst_pin=0xFF,
                display_instance=0,
                flags=0,
                poll_interval_ms=100,
                touch_data_start_byte=touch_data_start_byte,
                reserved=b"\x00" * 4,
            )
        ],
    )


def make_zeroconf_info(
    host: str = "192.168.1.50",
    port: int | None = 1234,
    properties: dict[str, str] | None = None,
) -> ZeroconfServiceInfo:
    """Return an mDNS record for an OpenDisplay tag."""
    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        port=port,
        hostname="opendisplay-1234.local.",
        type="_opendisplay._tcp.local.",
        name="OpenDisplay 1234._opendisplay._tcp.local.",
        properties={"mac": TEST_ADDRESS} if properties is None else properties,
    )


ZEROCONF_INFO = make_zeroconf_info()


def make_nfc_device_config(enabled: bool = True, sleepy: bool = False) -> GlobalConfig:
    """Return a GlobalConfig with one NFC tag, enabled via flags bit 0."""
    return GlobalConfig(
        system=DEVICE_CONFIG.system,
        manufacturer=DEVICE_CONFIG.manufacturer,
        power=make_sleepy_device_config().power if sleepy else DEVICE_CONFIG.power,
        displays=DEVICE_CONFIG.displays,
        nfc_configs=[
            NfcConfig(
                instance_number=0,
                nfc_ic_type=0,
                bus_instance=0,
                flags=0x01 if enabled else 0x00,
                field_detect_pin=0xFF,
                field_detect_mode=0,
                field_detect_active=0,
                field_detect_debounce_ms=0,
                power_pin=0xFF,
                power_active=0,
                power_on_delay_ms=0,
                power_off_delay_ms=0,
                adv_button_byte_index=0,
                adv_button_button_id=0,
                reserved_pin_1=0xFF,
                reserved_pin_2=0xFF,
                reserved=b"\x00" * 4,
            )
        ],
    )


OTA_REPO = "OpenDisplay/Firmware_Silabs"
GITHUB_LATEST = f"https://api.github.com/repos/{OTA_REPO}/releases/latest"


def make_ota_device_config(sleepy: bool = False) -> GlobalConfig:
    """Return a GlobalConfig for an EFR32BG22, the IC that supports BLE OTA."""
    return GlobalConfig(
        system=replace(DEVICE_CONFIG.system, ic_type=ICType.EFR32BG22),
        manufacturer=DEVICE_CONFIG.manufacturer,
        power=make_sleepy_device_config().power if sleepy else DEVICE_CONFIG.power,
        displays=DEVICE_CONFIG.displays,
    )


def make_notifier_device_config(
    leds: bool = True, buzzers: bool = True, sleepy: bool = False
) -> GlobalConfig:
    """Return a GlobalConfig with LED and/or buzzer hardware fitted."""
    return GlobalConfig(
        system=DEVICE_CONFIG.system,
        manufacturer=DEVICE_CONFIG.manufacturer,
        power=make_sleepy_device_config().power if sleepy else DEVICE_CONFIG.power,
        displays=DEVICE_CONFIG.displays,
        leds=[
            LedConfig(
                instance_number=0,
                led_type=1,
                led_1_r=1,
                led_2_g=2,
                led_3_b=3,
                led_4=0xFF,
                led_flags=0,
                reserved=b"\x00" * 4,
            )
        ]
        if leds
        else [],
        buzzers=[
            PassiveBuzzer(
                instance_number=0,
                drive_pin=4,
                enable_pin=0xFF,
                flags=0,
                duty_percent=50,
                reserved=b"\x00" * 4,
            )
        ]
        if buzzers
        else [],
    )


def make_wifi_device_config() -> GlobalConfig:
    """Return a GlobalConfig carrying a wifi_config packet."""
    return GlobalConfig(
        system=DEVICE_CONFIG.system,
        manufacturer=DEVICE_CONFIG.manufacturer,
        power=DEVICE_CONFIG.power,
        displays=DEVICE_CONFIG.displays,
        wifi_config=WifiConfig(
            ssid=b"test-ssid".ljust(32, b"\x00"),
            password=b"test-password".ljust(64, b"\x00"),
            encryption_type=0,
            server_url=b"http://example.invalid/".ljust(64, b"\x00"),
            server_port=80,
            reserved=b"\x00" * 8,
        ),
    )


def make_button_device_config(binary_inputs: list[BinaryInputs]) -> GlobalConfig:
    """Return a GlobalConfig with the given binary_inputs list."""
    return GlobalConfig(
        system=DEVICE_CONFIG.system,
        manufacturer=DEVICE_CONFIG.manufacturer,
        power=DEVICE_CONFIG.power,
        displays=DEVICE_CONFIG.displays,
        binary_inputs=binary_inputs,
    )


VALID_SERVICE_INFO = make_service_info()


_UNSET = object()


@contextmanager
def connects_to(device: object) -> Iterator[MagicMock]:
    """Patch the transport so a connection attempt yields ``device``.

    Yields the OpenDisplayDevice mock, whose call kwargs say which transport
    was chosen: a WiFi attempt passes ``host``, a BLE one ``mac_address``.
    """
    with _patched_transport(lambda **kw: _async_ctx(device)) as constructor:
        yield constructor


@contextmanager
def connection_fails(error: Exception) -> Iterator[MagicMock]:
    """Patch the transport so every connection attempt raises ``error``."""

    def _factory(**kwargs):
        return _async_ctx(error=error)

    with _patched_transport(_factory) as constructor:
        yield constructor


@contextmanager
def connects_via(factory) -> Iterator[MagicMock]:
    """Patch the transport with a caller-supplied device factory."""
    with _patched_transport(factory) as constructor:
        yield constructor


def _async_ctx(device: object = None, error: Exception | None = None):
    """Return an async context manager yielding ``device`` or raising ``error``."""
    ctx = MagicMock()

    async def _aenter(*_args):
        if error is not None:
            raise error
        return device

    async def _aexit(*_args):
        return False

    ctx.__aenter__ = _aenter
    ctx.__aexit__ = _aexit
    return ctx


@contextmanager
def ble_unreachable() -> Iterator[None]:
    """Patch the transport so no connectable BLE device can be found."""
    from custom_components.opendisplay import transport as transport_mod

    with (
        patch.object(transport_mod, "OpenDisplayDevice"),
        patch.object(transport_mod, "async_ble_device_from_address", return_value=None),
    ):
        yield


@contextmanager
def _patched_transport(factory, ble_device: object = _UNSET) -> Iterator[MagicMock]:
    """Patch both of the transport module's connection touchpoints."""
    from custom_components.opendisplay import transport as transport_mod

    with (
        patch.object(
            transport_mod,
            "async_ble_device_from_address",
            return_value=MagicMock() if ble_device is _UNSET else ble_device,
        ),
        patch.object(
            transport_mod, "OpenDisplayDevice", side_effect=factory
        ) as constructor,
    ):
        yield constructor
