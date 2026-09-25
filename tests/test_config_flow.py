"""Test the OpenDisplay config flow."""

import asyncio
from collections.abc import Generator
from datetime import timedelta
from unittest.mock import MagicMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from opendisplay import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    OpenDisplayError,
    build_landing_url,
)
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.opendisplay.const import (
    CONF_ENCRYPTION_KEY,
    CONF_HOST,
    CONF_PORT,
    CONF_TLS,
    CONNECT_PROBE_DEADLINE_S,
    DEFAULT_TLS_PORT,
    DOMAIN,
)

from . import (
    ENCRYPTION_KEY,
    NOT_OPENDISPLAY_SERVICE_INFO,
    TEST_ADDRESS,
    VALID_SERVICE_INFO,
    ZEROCONF_INFO,
    make_service_info,
    make_zeroconf_info,
)


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[None]:
    """Prevent the integration from actually setting up after config flow."""
    with patch(
        "custom_components.opendisplay.async_setup_entry",
        return_value=True,
    ):
        yield


async def test_bluetooth_discovery(hass: HomeAssistant) -> None:
    """Test discovery via Bluetooth with a valid device."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=VALID_SERVICE_INFO,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "OpenDisplay 1234"
    assert result["data"] == {}
    assert result["result"].unique_id == "AA:BB:CC:DD:EE:FF"


async def test_bluetooth_discovery_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test discovery aborts when device is already configured."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=VALID_SERVICE_INFO,
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_bluetooth_discovery_already_in_progress(hass: HomeAssistant) -> None:
    """Test discovery aborts when same device flow is in progress."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=VALID_SERVICE_INFO,
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=VALID_SERVICE_INFO,
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"


@pytest.mark.parametrize(
    ("exception", "expected_reason"),
    [
        (BLEConnectionError("test"), "cannot_connect"),
        (BLETimeoutError("test"), "cannot_connect"),
        (OpenDisplayError("test"), "cannot_connect"),
        (RuntimeError("test"), "unknown"),
    ],
)
async def test_bluetooth_confirm_connection_error(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
    exception: Exception,
    expected_reason: str,
) -> None:
    """Test the confirm step shows an error and allows a retry when connecting fails."""
    mock_opendisplay_device.__aenter__.side_effect = exception

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=VALID_SERVICE_INFO,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    assert result["errors"] == {"base": expected_reason}

    mock_opendisplay_device.__aenter__.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_bluetooth_discovery_does_not_connect(
    hass: HomeAssistant,
    mock_opendisplay_device_class: MagicMock,
) -> None:
    """Test that discovery alone never opens a connection to the device."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=VALID_SERVICE_INFO,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    mock_opendisplay_device_class.assert_not_called()


async def test_bluetooth_confirm_ble_device_not_found(
    hass: HomeAssistant,
) -> None:
    """Test the confirm step reports an error when the BLE device is not found."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=VALID_SERVICE_INFO,
    )

    with patch(
        "custom_components.opendisplay.config_flow.async_ble_device_from_address",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_bluetooth_confirm_connection_timeout(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
) -> None:
    """Test that a device which stops responding does not hang the flow."""

    async def _never_returns(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    mock_opendisplay_device.read_firmware_version.side_effect = _never_returns

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=VALID_SERVICE_INFO,
    )

    task = hass.async_create_task(
        hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    )
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=CONNECT_PROBE_DEADLINE_S)
    )
    result = await task

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_with_devices(hass: HomeAssistant) -> None:
    """Test user step with discovered devices."""
    with patch(
        "custom_components.opendisplay.config_flow.async_discovered_service_info",
        return_value=[VALID_SERVICE_INFO],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"address": "AA:BB:CC:DD:EE:FF"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "OpenDisplay 1234"
    assert result["data"] == {}
    assert result["result"].unique_id == "AA:BB:CC:DD:EE:FF"


async def test_user_step_no_devices(hass: HomeAssistant) -> None:
    """Test user step when no devices are discovered."""
    with patch(
        "custom_components.opendisplay.config_flow.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_user_step_filters_unsupported(hass: HomeAssistant) -> None:
    """Test user step filters out unsupported devices."""
    with patch(
        "custom_components.opendisplay.config_flow.async_discovered_service_info",
        return_value=[NOT_OPENDISPLAY_SERVICE_INFO],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


@pytest.mark.parametrize(
    ("exception", "expected_error"),
    [
        (BLEConnectionError("test"), "cannot_connect"),
        (BLETimeoutError("test"), "cannot_connect"),
        (OpenDisplayError("test"), "cannot_connect"),
        (RuntimeError("test"), "unknown"),
    ],
)
async def test_user_step_connection_error(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
    exception: Exception,
    expected_error: str,
) -> None:
    """Test user step handles connection and unexpected errors."""
    with patch(
        "custom_components.opendisplay.config_flow.async_discovered_service_info",
        return_value=[VALID_SERVICE_INFO],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )

    assert result["type"] is FlowResultType.FORM

    mock_opendisplay_device.__aenter__.side_effect = exception
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"address": "AA:BB:CC:DD:EE:FF"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}

    mock_opendisplay_device.__aenter__.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"address": "AA:BB:CC:DD:EE:FF"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_step_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test user step aborts when device is already configured."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.opendisplay.config_flow.async_discovered_service_info",
        return_value=[VALID_SERVICE_INFO],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )

    # Device is filtered out since it's already configured
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_bluetooth_discovery_encrypted_device(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
) -> None:
    """Test Bluetooth discovery prompts for key when device requires encryption."""
    mock_opendisplay_device.__aenter__.side_effect = [
        AuthenticationRequiredError("auth required"),
        mock_opendisplay_device,
    ]

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=VALID_SERVICE_INFO,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "encryption_key"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: ENCRYPTION_KEY},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ENCRYPTION_KEY: ENCRYPTION_KEY}


async def test_bluetooth_discovery_encrypted_invalid_key_format(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
) -> None:
    """Test encryption_key step shows error on invalid key format."""
    mock_opendisplay_device.__aenter__.side_effect = [
        AuthenticationRequiredError("auth required"),
        mock_opendisplay_device,
    ]

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=VALID_SERVICE_INFO,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "encryption_key"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: "tooshort"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "encryption_key"
    assert result["errors"] == {CONF_ENCRYPTION_KEY: "invalid_key_format"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: ENCRYPTION_KEY},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_bluetooth_discovery_encrypted_wrong_key(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
) -> None:
    """Test encryption_key step shows error on wrong key, then succeeds."""
    mock_opendisplay_device.__aenter__.side_effect = [
        AuthenticationRequiredError("auth required"),
        AuthenticationFailedError("wrong key"),
        mock_opendisplay_device,
    ]

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=VALID_SERVICE_INFO,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "encryption_key"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: ENCRYPTION_KEY},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENCRYPTION_KEY: "invalid_auth"}

    mock_opendisplay_device.__aenter__.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: ENCRYPTION_KEY},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ENCRYPTION_KEY: ENCRYPTION_KEY}


async def test_user_step_encrypted_device(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
) -> None:
    """Test user step prompts for key when device requires encryption."""
    mock_opendisplay_device.__aenter__.side_effect = [
        AuthenticationRequiredError("auth required"),
        mock_opendisplay_device,
    ]

    with patch(
        "custom_components.opendisplay.config_flow.async_discovered_service_info",
        return_value=[VALID_SERVICE_INFO],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"address": "AA:BB:CC:DD:EE:FF"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "encryption_key"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: ENCRYPTION_KEY},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ENCRYPTION_KEY: ENCRYPTION_KEY}


async def test_reauth_update_key(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
    mock_encrypted_config_entry: MockConfigEntry,
) -> None:
    """Test reauth flow updates the encryption key."""
    mock_encrypted_config_entry.add_to_hass(hass)
    new_key = "11223344556677881122334455667788"

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": mock_encrypted_config_entry.entry_id,
        },
        data=mock_encrypted_config_entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: new_key},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_encrypted_config_entry.data[CONF_ENCRYPTION_KEY] == new_key


async def test_reauth_remove_key(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
    mock_encrypted_config_entry: MockConfigEntry,
) -> None:
    """Test reauth flow removes the encryption key when left blank."""
    mock_encrypted_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": mock_encrypted_config_entry.entry_id,
        },
        data=mock_encrypted_config_entry.data,
    )
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: ""},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert CONF_ENCRYPTION_KEY not in mock_encrypted_config_entry.data


async def test_reauth_wrong_key(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
    mock_encrypted_config_entry: MockConfigEntry,
) -> None:
    """Test reauth form shows error for wrong key, then succeeds."""
    mock_encrypted_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": mock_encrypted_config_entry.entry_id,
        },
        data=mock_encrypted_config_entry.data,
    )
    assert result["step_id"] == "reauth_confirm"

    mock_opendisplay_device.__aenter__.side_effect = [
        AuthenticationFailedError("wrong key"),
        mock_opendisplay_device,
    ]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: ENCRYPTION_KEY},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENCRYPTION_KEY: "invalid_auth"}

    mock_opendisplay_device.__aenter__.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: ENCRYPTION_KEY},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reauth_invalid_key_format(
    hass: HomeAssistant,
    mock_encrypted_config_entry: MockConfigEntry,
) -> None:
    """Test reauth form shows error for a malformed encryption key."""
    mock_encrypted_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": mock_encrypted_config_entry.entry_id,
        },
        data=mock_encrypted_config_entry.data,
    )
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: "notvalidhex!"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENCRYPTION_KEY: "invalid_key_format"}


# --- WiFi (mDNS) discovery -------------------------------------------------


async def test_zeroconf_discovery_creates_a_wifi_entry(hass: HomeAssistant) -> None:
    """A tag discovered over WiFi first is confirmed and stored with its endpoint."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"

    with patch(
        "custom_components.opendisplay.config_flow.OpenDisplayConfigFlow._async_test_connection_tcp"
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_HOST: "192.168.1.50",
        CONF_PORT: 1234,
        CONF_TLS: False,
    }
    assert result["result"].unique_id == TEST_ADDRESS


async def test_zeroconf_without_a_mac_is_ignored(hass: HomeAssistant) -> None:
    """Identity is anchored on the BLE MAC; a record without one cannot be matched."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=make_zeroconf_info(properties={}),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_mac"


async def test_zeroconf_adds_the_lan_endpoint_to_an_existing_ble_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A BLE-configured tag gains host/port/tls when it is later seen over mDNS.

    This is what lets a tag set up over Bluetooth start using WiFi delivery.
    """
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config_entry.data[CONF_HOST] == "192.168.1.50"
    assert mock_config_entry.data[CONF_PORT] == 1234
    assert mock_config_entry.data[CONF_TLS] is False


async def test_zeroconf_tls_record_uses_the_tls_port_default(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A TLS record with no explicit port falls back to the TLS default."""
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=make_zeroconf_info(
            port=None, properties={"mac": TEST_ADDRESS, "tls": "1"}
        ),
    )

    assert mock_config_entry.data[CONF_TLS] is True
    assert mock_config_entry.data[CONF_PORT] == DEFAULT_TLS_PORT


async def test_zeroconf_confirm_reports_an_unreachable_endpoint(
    hass: HomeAssistant,
) -> None:
    """A probe failure keeps the user on the form rather than storing a dead host."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )

    with patch(
        "custom_components.opendisplay.config_flow.OpenDisplayConfigFlow._async_test_connection_tcp",
        side_effect=OSError,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


# --- QR-code landing URL as encryption key ---------------------------------


def _landing_url(device_id: bytes, key_hex: str | None) -> str:
    """Build a firmware-style landing URL for the given device id and key."""
    return build_landing_url(
        0, device_id, bytes.fromhex(key_hex) if key_hex else None, 3
    )


async def _start_encrypted_flow_for(hass: HomeAssistant, name: str) -> dict:
    """Discover an encrypted device with ``name`` and land on the key step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=make_service_info(name=name),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "encryption_key"
    return result


async def test_encryption_key_from_qr_url(
    hass: HomeAssistant, mock_opendisplay_device: MagicMock
) -> None:
    """A scanned/pasted landing URL for this device yields its key."""
    mock_opendisplay_device.__aenter__.side_effect = [
        AuthenticationRequiredError("auth required"),
        mock_opendisplay_device,
    ]
    result = await _start_encrypted_flow_for(hass, "OD4B3F63")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: _landing_url(b"\x4b\x3f\x63", ENCRYPTION_KEY)},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ENCRYPTION_KEY: ENCRYPTION_KEY}


@pytest.mark.parametrize(
    ("url", "error"),
    [
        (_landing_url(b"\x01\x02\x03", ENCRYPTION_KEY), "qr_wrong_device"),
        (_landing_url(b"\x4b\x3f\x63", None), "qr_key_hidden"),
        ("https://opendisplay.org/l/?AAAA", "invalid_key_format"),
    ],
)
async def test_encryption_key_bad_qr_url(
    hass: HomeAssistant, mock_opendisplay_device: MagicMock, url: str, error: str
) -> None:
    """Wrong-device, keyless and malformed QR links get specific errors."""
    mock_opendisplay_device.__aenter__.side_effect = [
        AuthenticationRequiredError("auth required"),
    ]
    result = await _start_encrypted_flow_for(hass, "OD4B3F63")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_ENCRYPTION_KEY: url}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENCRYPTION_KEY: error}


async def test_reauth_key_from_qr_url(
    hass: HomeAssistant,
    mock_opendisplay_device: MagicMock,
    mock_encrypted_config_entry: MockConfigEntry,
) -> None:
    """Reauth accepts a landing URL too (entry title isn't OD######: no id check)."""
    mock_encrypted_config_entry.add_to_hass(hass)
    new_key = "00112233445566778899aabbccddeeff"

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": mock_encrypted_config_entry.entry_id,
        },
        data=mock_encrypted_config_entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENCRYPTION_KEY: _landing_url(b"\x4b\x3f\x63", new_key)},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_encrypted_config_entry.data[CONF_ENCRYPTION_KEY] == new_key
