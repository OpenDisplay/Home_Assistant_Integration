"""The display's SEC2 keys, kept in its config entry (py-opendisplay's ``KeyStore``).

``opendisplay.connect()`` tries these keys and, on a firmware 3.0 display whose key was
migrated from 2.x, replaces that key with a device-generated one (SECURITY_V2 § 10.5). It
stages the new key here and sends ROTATE_CONFIRM only after the key reads back, so a rotation
cut short leaves both keys to try. The entry holds one display, so the device name the library
passes is not needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from opendisplay.sec2.errors import Sec2StorageError
from opendisplay.sec2.keystore import MAX_TRY

from .const import CONF_DEVICE_UID, CONF_ENCRYPTION_KEY, CONF_KEY_CANDIDATES, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_ROTATION = "rotation"


def key_rotated_issue_id(entry: ConfigEntry) -> str:
    """Return the repair issue id that announces a rotated key for ``entry``."""
    return f"key_rotated_{entry.entry_id}"


class EntryKeyStore:
    """SEC2 keys of the display behind one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Wrap ``entry``; every change is written to its data at once."""
        self._hass = hass
        self._entry = entry
        self._staged_rotation: str | None = None

    def _candidates(self) -> list[str]:
        return list(self._entry.data.get(CONF_KEY_CANDIDATES, []))

    def _update(self, **changes: Any) -> None:
        data = {**self._entry.data, **changes}
        for key, value in changes.items():
            if value is None:
                data.pop(key, None)
        self._hass.config_entries.async_update_entry(self._entry, data=data)

    def keys_to_try(self, name: str) -> list[bytes]:
        """Return the confirmed key, then the candidates newest first; at most three."""
        confirmed = self._entry.data.get(CONF_ENCRYPTION_KEY)
        hexes = ([confirmed] if confirmed else []) + self._candidates()[::-1]
        return [bytes.fromhex(k) for k in list(dict.fromkeys(hexes))[:MAX_TRY]]

    def uid(self, name: str) -> bytes | None:
        """Return the uid the confirmed key is bound to."""
        uid = self._entry.data.get(CONF_DEVICE_UID)
        return bytes.fromhex(uid) if uid else None

    def add_candidate(self, name: str, key: bytes, source: str) -> None:
        """Remember a key that might work."""
        hex_key = key.hex()
        candidates = self._candidates()
        if (
            hex_key != self._entry.data.get(CONF_ENCRYPTION_KEY)
            and hex_key not in candidates
        ):
            self._update(**{CONF_KEY_CANDIDATES: [*candidates, hex_key]})

    def stage(self, name: str, key: bytes, kind: str) -> None:
        """Store a key the display may now hold, and prove it is stored."""
        hex_key = key.hex()
        candidates = [k for k in self._candidates() if k != hex_key]
        self._update(**{CONF_KEY_CANDIDATES: [*candidates, hex_key]})
        if hex_key not in self._candidates():
            raise Sec2StorageError(
                "the staged key did not read back from the config entry"
            )
        if kind == _ROTATION:
            self._staged_rotation = hex_key

    def promote(self, name: str, key: bytes, *, uid: bytes | None = None) -> None:
        """``key`` opened a session: it is the display's key now, every other key goes."""
        hex_key = key.hex()
        changes: dict[str, Any] = {
            CONF_ENCRYPTION_KEY: hex_key,
            CONF_KEY_CANDIDATES: [],
        }
        if uid is not None:
            changes[CONF_DEVICE_UID] = uid.hex()
        self._update(**changes)
        if self._staged_rotation == hex_key:
            self._staged_rotation = None
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                key_rotated_issue_id(self._entry),
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="key_rotated",
                translation_placeholders={"name": self._entry.title},
                data={"entry_id": self._entry.entry_id},
            )

    def forget(self, name: str) -> None:
        """Remove every key (the display was unclaimed with this key)."""
        self._update(
            **{
                CONF_ENCRYPTION_KEY: None,
                CONF_KEY_CANDIDATES: None,
                CONF_DEVICE_UID: None,
            }
        )
