"""Accept an encryption key as hex or as the device's on-screen QR landing URL.

The firmware shows a QR code linking to ``https://opendisplay.org/l/?<payload>``;
the payload carries the device id ("OD######") and, when the device's
``show_key_on_screen`` security flag is set, its AES key. The config flow key
fields accept either the bare 32-hex key or that URL.
"""

import re

import voluptuous as vol

from opendisplay import parse_landing_url

_HEX_KEY_VALIDATOR = vol.All(str.strip, str.lower, vol.Match(r"^[0-9a-f]{32}$"))
_OD_NAME_RE = re.compile(r"^OD[0-9A-F]{6}$")


class InvalidKeyFormat(ValueError):
    """Input is neither a 32-hex key nor an OpenDisplay landing URL."""


class QrCodeKeyHidden(ValueError):
    """Landing URL decoded, but its key slot is all zeros.

    The firmware zero-fills the slot when encryption is off or when the key is
    hidden. The flow only asks for a key after the device demanded one, so
    here it means hidden.
    """


class QrCodeWrongDevice(ValueError):
    """Landing URL belongs to a different device than the one being set up."""


def resolve_encryption_key(value: str, device_name: str | None) -> str:
    """Return the normalised 32-hex key from a hex string or a landing URL.

    ``device_name`` is the name of the device being configured; when it has the
    ``OD######`` form, a QR code for a different device is rejected. Other names
    (a renamed entry title) skip the check; the connection probe still rejects
    a wrong key.

    Raises:
        InvalidKeyFormat, QrCodeKeyHidden, QrCodeWrongDevice.

    """
    value = value.strip()
    if "://" not in value and not value.lower().startswith("opendisplay.org"):
        try:
            return str(_HEX_KEY_VALIDATOR(value))
        except vol.Invalid as err:
            raise InvalidKeyFormat from err

    try:
        info = parse_landing_url(value if "://" in value else f"https://{value}")
    except ValueError as err:
        raise InvalidKeyFormat from err

    if (
        device_name is not None
        and _OD_NAME_RE.match(device_name)
        and info.device_name != device_name
    ):
        raise QrCodeWrongDevice(info.device_name)
    if info.encryption_key is None:
        raise QrCodeKeyHidden
    return info.encryption_key.hex()
