"""Config flow for OpenDisplay integration."""

import asyncio
from collections.abc import Mapping
import logging
from typing import TYPE_CHECKING, Any

from opendisplay import (
    MANUFACTURER_ID,
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    OpenDisplayDevice,
    OpenDisplayError,
)
import voluptuous as vol

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
)

from .ble_lock import ble_connection
from .const import (
    CONF_BLOCKS_PER_ACK,
    CONF_ENCRYPTION_KEY,
    CONF_MAX_QUEUE_SIZE,
    CONF_MISSED_CYCLES,
    CONF_PROBE_BEFORE_QUEUE,
    CONF_QUEUE_TIMEOUT_HOURS,
    CONF_SLEEP_MODE,
    CONNECT_PROBE_DEADLINE_S,
    DEFAULT_BLOCKS_PER_ACK,
    DEFAULT_MAX_QUEUE_SIZE,
    DEFAULT_MISSED_CYCLES,
    DEFAULT_PROBE_BEFORE_QUEUE,
    DEFAULT_QUEUE_TIMEOUT_HOURS,
    DEFAULT_SLEEP_MODE,
    DOMAIN,
    SLEEP_MODE_AUTO,
    SLEEP_MODE_OFF,
    SLEEP_MODE_ON,
)

_LOGGER = logging.getLogger(__name__)


def _options_schema() -> vol.Schema:
    """Return the options-flow schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_SLEEP_MODE, default=DEFAULT_SLEEP_MODE
            ): SelectSelector(
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
            vol.Required(
                CONF_BLOCKS_PER_ACK, default=DEFAULT_BLOCKS_PER_ACK
            ): vol.All(
                NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=32, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Coerce(int),
            ),
            vol.Required(
                CONF_MAX_QUEUE_SIZE, default=DEFAULT_MAX_QUEUE_SIZE
            ): vol.All(
                NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=32, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Coerce(int),
            ),
        }
    )


_ENCRYPTION_KEY_VALIDATOR = vol.All(str.strip, str.lower, vol.Match(r"^[0-9a-f]{32}$"))


class OpenDisplayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenDisplay."""

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

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
                async with ble_connection(
                    address, "connection probe (config flow)"
                ), OpenDisplayDevice(
                    mac_address=address,
                    ble_device=ble_device,
                    encryption_key=encryption_key,
                ) as device:
                    await device.read_firmware_version()
        except TimeoutError as err:
            raise BLEConnectionError(
                f"Connection probe exceeded {CONNECT_PROBE_DEADLINE_S:.0f}s"
            ) from err

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
                return self.async_create_entry(
                    title=self._discovery_info.name, data={}
                )

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
            try:
                key: str = _ENCRYPTION_KEY_VALIDATOR(user_input[CONF_ENCRYPTION_KEY])
            except vol.Invalid:
                errors[CONF_ENCRYPTION_KEY] = "invalid_key_format"
            else:
                if TYPE_CHECKING:
                    assert self.unique_id is not None
                if await self._async_try_connection(
                    self.unique_id, bytes.fromhex(key), errors
                ):
                    return self.async_create_entry(
                        title=name,
                        data={CONF_ENCRYPTION_KEY: key},
                    )

        return self.async_show_form(
            step_id="encryption_key",
            data_schema=vol.Schema({vol.Required(CONF_ENCRYPTION_KEY): str}),
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
            if user_input[CONF_ENCRYPTION_KEY].strip():
                try:
                    key = _ENCRYPTION_KEY_VALIDATOR(user_input[CONF_ENCRYPTION_KEY])
                except vol.Invalid:
                    errors[CONF_ENCRYPTION_KEY] = "invalid_key_format"

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

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Optional(CONF_ENCRYPTION_KEY, default=""): str}
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
