"""Config flow for OpenDisplay integration."""

import asyncio
from collections.abc import Mapping
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import voluptuous as vol

from opendisplay import (
    MANUFACTURER_ID,
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    OpenDisplayDevice,
    OpenDisplayError,
)

from .ble_lock import ble_connection
from .const import (
    CONF_BLOCKS_PER_ACK,
    CONF_ENCRYPTION_KEY,
    CONF_HOST,
    CONF_MAX_QUEUE_SIZE,
    CONF_MISSED_CYCLES,
    CONF_PORT,
    CONF_PROBE_BEFORE_QUEUE,
    CONF_QUEUE_TIMEOUT_HOURS,
    CONF_SLEEP_MODE,
    CONF_TLS,
    CONNECT_PROBE_DEADLINE_S,
    DEFAULT_BLOCKS_PER_ACK,
    DEFAULT_LAN_PORT,
    DEFAULT_MAX_QUEUE_SIZE,
    DEFAULT_MISSED_CYCLES,
    DEFAULT_PROBE_BEFORE_QUEUE,
    DEFAULT_QUEUE_TIMEOUT_HOURS,
    DEFAULT_SLEEP_MODE,
    DEFAULT_TLS_PORT,
    DOMAIN,
    SLEEP_MODE_AUTO,
    SLEEP_MODE_OFF,
    SLEEP_MODE_ON,
    TCP_CONNECT_TIMEOUT_S,
)
from .frontend import async_register_frontend
from .qr import (
    InvalidKeyFormat,
    QrCodeKeyHidden,
    QrCodeWrongDevice,
    resolve_encryption_key,
)
from .transport import note_mdns_seen

_LOGGER = logging.getLogger(__name__)


def _txt_str(properties: Mapping[str, Any], key: str) -> str | None:
    """Read a zeroconf TXT value as a stripped str (bytes tolerated), or None."""
    value = properties.get(key)
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    text = str(value).strip()
    return text or None


def _txt_bool(properties: Mapping[str, Any], key: str) -> bool:
    """Interpret a zeroconf TXT flag as boolean (``1``/``true``/``yes`` → True)."""
    value = _txt_str(properties, key)
    return value is not None and value.lower() in ("1", "true", "yes")


def _options_schema() -> vol.Schema:
    """Return the options-flow schema."""
    return vol.Schema(
        {
            vol.Required(CONF_SLEEP_MODE, default=DEFAULT_SLEEP_MODE): SelectSelector(
                SelectSelectorConfig(
                    options=[SLEEP_MODE_AUTO, SLEEP_MODE_ON, SLEEP_MODE_OFF],
                    translation_key="sleep_mode",
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(CONF_MISSED_CYCLES, default=DEFAULT_MISSED_CYCLES): vol.All(
                NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=100, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Coerce(int),
            ),
            vol.Required(
                CONF_QUEUE_TIMEOUT_HOURS, default=DEFAULT_QUEUE_TIMEOUT_HOURS
            ): vol.All(
                NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=168,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="h",
                    )
                ),
                vol.Coerce(int),
            ),
            vol.Required(
                CONF_PROBE_BEFORE_QUEUE, default=DEFAULT_PROBE_BEFORE_QUEUE
            ): BooleanSelector(),
            vol.Required(CONF_BLOCKS_PER_ACK, default=DEFAULT_BLOCKS_PER_ACK): vol.All(
                NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=32, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Coerce(int),
            ),
            vol.Required(CONF_MAX_QUEUE_SIZE, default=DEFAULT_MAX_QUEUE_SIZE): vol.All(
                NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=32, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Coerce(int),
            ),
        }
    )


def _resolve_key_or_error(
    value: str, device_name: str | None, errors: dict[str, str]
) -> str | None:
    """Resolve a hex key or QR landing URL; on failure fill ``errors``."""
    try:
        return resolve_encryption_key(value, device_name)
    except QrCodeWrongDevice:
        errors[CONF_ENCRYPTION_KEY] = "qr_wrong_device"
    except QrCodeKeyHidden:
        errors[CONF_ENCRYPTION_KEY] = "qr_key_hidden"
    except InvalidKeyFormat:
        errors[CONF_ENCRYPTION_KEY] = "invalid_key_format"
    return None


class OpenDisplayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenDisplay."""

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        # Zeroconf (WiFi/LAN) discovery of a not-yet-configured device.
        self._zeroconf_host: str | None = None
        self._zeroconf_port: int = DEFAULT_LAN_PORT
        self._zeroconf_tls: bool = False
        self._zeroconf_name: str = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return OpenDisplayOptionsFlow()

    async def _async_test_connection(
        self, address: str, encryption_key: bytes | None = None
    ) -> None:
        """Connect to the device and verify it responds."""
        ble_device = async_ble_device_from_address(self.hass, address, connectable=True)
        if ble_device is None:
            raise BLEConnectionError(f"Could not find connectable device for {address}")

        # Bound the whole probe (connect + auto-interrogate + firmware read) so a
        # wedged BLE link can't freeze the config dialog. A breach is surfaced as
        # BLEConnectionError, which the callers' existing OpenDisplayError handling
        # maps to "cannot_connect"; AuthenticationRequiredError still propagates.
        try:
            async with asyncio.timeout(CONNECT_PROBE_DEADLINE_S):
                async with (
                    ble_connection(address, "connection probe (config flow)"),
                    OpenDisplayDevice(
                        mac_address=address,
                        ble_device=ble_device,
                        encryption_key=encryption_key,
                    ) as device,
                ):
                    await device.read_firmware_version()
        except TimeoutError as err:
            raise BLEConnectionError(
                f"Connection probe exceeded {CONNECT_PROBE_DEADLINE_S:.0f}s"
            ) from err

    async def _async_test_connection_tcp(self, host: str, port: int) -> None:
        """Probe a TCP/LAN endpoint for reachability (zeroconf confirm step).

        A lightweight reachability check: open a TCP connection with a short
        timeout, then close it immediately. It does not speak the protocol (no
        interrogation) — full interrogation happens at entry setup, over WiFi or a
        BLE fallback. Deliberately separate from the BLE-only
        ``_async_test_connection`` so the two transports' probes stay independent.

        Raises:
            OSError / TimeoutError: if the endpoint is unreachable.

        """
        async with asyncio.timeout(TCP_CONNECT_TIMEOUT_S):
            _reader, writer = await asyncio.open_connection(host, port)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a WiFi (mDNS) discovery of an OpenDisplay device.

        The device is identified by its BLE MAC (the ``mac`` TXT key), so a
        WiFi-discovered tag dedups onto its existing BLE entry (and vice versa):
        the unique_id is the uppercase-colon MAC, matching the raw BlueZ form the
        bluetooth step stores. An already-configured entry just gains
        host/port/tls (and an mDNS-presence timestamp so the resolver can prefer
        WiFi); a brand-new device goes through a confirm step.
        """
        properties = discovery_info.properties
        mac = _txt_str(properties, "mac")
        if not mac:
            # Identity is anchored on the BLE MAC (SECTION 9); without it the
            # device can't be correlated with a BLE entry — ignore the record.
            return self.async_abort(reason="no_mac")

        unique_id = mac.upper()
        await self.async_set_unique_id(unique_id)

        tls = _txt_bool(properties, "tls")
        host = discovery_info.host
        port = discovery_info.port or (DEFAULT_TLS_PORT if tls else DEFAULT_LAN_PORT)

        # Feed mDNS presence to an already-loaded entry before the abort below
        # short-circuits: this doubles as a "device seen" wake trigger (union of
        # BLE advert + mDNS) and refreshes the WiFi-preference freshness window.
        self._async_note_mdns_for_configured(unique_id)

        # Existing entry (BLE or WiFi): merge in the live host/port/tls and abort.
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: host, CONF_PORT: port, CONF_TLS: tls}
        )

        # New device discovered over WiFi first — confirm, then create the entry.
        self._zeroconf_host = host
        self._zeroconf_port = port
        self._zeroconf_tls = tls
        self._zeroconf_name = (
            discovery_info.name.removesuffix("._opendisplay._tcp.local.") or host
        )
        self.context["title_placeholders"] = {"name": self._zeroconf_name}
        return await self.async_step_zeroconf_confirm()

    @callback
    def _async_note_mdns_for_configured(self, unique_id: str) -> None:
        """Record the mDNS sighting on a matching loaded entry, if any."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.unique_id == unique_id:
                note_mdns_seen(entry)
                return

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a WiFi-discovered device and create its entry."""
        assert self._zeroconf_host is not None
        errors: dict[str, str] = {}
        name = self.context["title_placeholders"]["name"]

        if user_input is not None:
            try:
                await self._async_test_connection_tcp(
                    self._zeroconf_host, self._zeroconf_port
                )
            except OSError, TimeoutError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error probing WiFi device")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_HOST: self._zeroconf_host,
                        CONF_PORT: self._zeroconf_port,
                        CONF_TLS: self._zeroconf_tls,
                    },
                )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={"name": name},
            errors=errors,
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle the Bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm discovery and connect to the device."""
        assert self._discovery_info is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self._async_test_connection(self._discovery_info.address)
            except AuthenticationRequiredError:
                _LOGGER.debug(
                    "%s: device requires an encryption key; prompting for one",
                    self._discovery_info.address,
                )
                return await self.async_step_encryption_key()
            except OpenDisplayError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=self._discovery_info.name, data={})

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=self.context["title_placeholders"],
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step to pick discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()

            try:
                await self._async_test_connection(address)
            except AuthenticationRequiredError:
                _LOGGER.debug(
                    "%s: device requires an encryption key; prompting for one", address
                )
                self.context["title_placeholders"] = {
                    "name": self._discovered_devices[address].name
                }
                return await self.async_step_encryption_key()
            except OpenDisplayError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=self._discovered_devices[address].name,
                    data={},
                )
        else:
            current_addresses = self._async_current_ids(include_ignore=False)
            for discovery_info in async_discovered_service_info(self.hass):
                address = discovery_info.address
                if address in current_addresses or address in self._discovered_devices:
                    continue
                if MANUFACTURER_ID in discovery_info.manufacturer_data:
                    self._discovered_devices[address] = discovery_info

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            addr: f"{info.name} ({addr})"
                            for addr, info in self._discovered_devices.items()
                        }
                    )
                }
            ),
            errors=errors,
        )

    async def _async_try_connection(
        self,
        address: str,
        encryption_key: bytes | None,
        errors: dict[str, str],
    ) -> bool:
        """Test connection, populate errors, and return True on success."""
        try:
            await self._async_test_connection(address, encryption_key)
        except (AuthenticationFailedError, AuthenticationRequiredError) as err:
            _LOGGER.debug("%s: encryption key rejected (%s)", address, err)
            errors[CONF_ENCRYPTION_KEY] = "invalid_auth"
        except OpenDisplayError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error")
            errors["base"] = "unknown"
        else:
            return True
        return False

    async def async_step_encryption_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the encryption key step."""
        errors: dict[str, str] = {}
        name: str = self.context["title_placeholders"]["name"]

        if user_input is not None:
            key = _resolve_key_or_error(user_input[CONF_ENCRYPTION_KEY], name, errors)
            if key is not None:
                if TYPE_CHECKING:
                    assert self.unique_id is not None
                if await self._async_try_connection(
                    self.unique_id, bytes.fromhex(key), errors
                ):
                    return self.async_create_entry(
                        title=name,
                        data={CONF_ENCRYPTION_KEY: key},
                    )
        else:
            # Lets the companion app offer a native "scan QR code" button.
            await async_register_frontend(self.hass)

        return self.async_show_form(
            step_id="encryption_key",
            data_schema=vol.Schema({vol.Required(CONF_ENCRYPTION_KEY): TextSelector()}),
            description_placeholders={"name": name},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            key: str | None = None
            if raw := user_input.get(CONF_ENCRYPTION_KEY, "").strip():
                key = _resolve_key_or_error(raw, reauth_entry.title, errors)

            if not errors:
                address = reauth_entry.unique_id
                if TYPE_CHECKING:
                    assert address is not None
                if await self._async_try_connection(
                    address, bytes.fromhex(key) if key is not None else None, errors
                ):
                    new_data = dict(reauth_entry.data)
                    if key is not None:
                        new_data[CONF_ENCRYPTION_KEY] = key
                    else:
                        new_data.pop(CONF_ENCRYPTION_KEY, None)
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data=new_data,
                    )
        else:
            await async_register_frontend(self.hass)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Optional(CONF_ENCRYPTION_KEY, default=""): TextSelector()}
            ),
            description_placeholders={"name": reauth_entry.title},
            errors=errors,
        )


class OpenDisplayOptionsFlow(OptionsFlowWithReload):
    """Handle deep-sleep options for an OpenDisplay device.

    Extends ``OptionsFlowWithReload`` so saving changed options automatically
    reloads the entry, re-resolving the sleep profile and re-applying the
    availability interval. No manual update listener is registered (mixing the
    two is disallowed).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the deep-sleep options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _options_schema(), self.config_entry.options
            ),
        )
