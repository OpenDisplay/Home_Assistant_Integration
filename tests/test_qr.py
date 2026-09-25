"""Test encryption-key input resolution (hex key or QR landing URL)."""

from opendisplay import build_landing_url
import pytest

from custom_components.opendisplay.qr import (
    InvalidKeyFormat,
    QrCodeKeyHidden,
    QrCodeWrongDevice,
    resolve_encryption_key,
)

KEY = bytes(range(16))


def landing_url(device_id: bytes = b"\x4b\x3f\x63", key: bytes | None = KEY) -> str:
    """Build a landing URL the way the firmware does."""
    return build_landing_url(0, device_id, key, 3)


class TestResolveEncryptionKey:
    """resolve_encryption_key accepts hex keys and landing URLs."""

    def test_hex_key_is_normalised(self) -> None:
        """Whitespace and case are normalised on a plain hex key."""
        assert resolve_encryption_key("  AABBCCDDEE112233AABBCCDDEE112233 ", None) == (
            "aabbccddee112233aabbccddee112233"
        )

    def test_landing_url(self) -> None:
        """The key is extracted from a landing URL for the matching device."""
        assert resolve_encryption_key(landing_url(), "OD4B3F63") == KEY.hex()

    def test_landing_url_without_scheme(self) -> None:
        """A retyped link without https:// still works."""
        url = landing_url().removeprefix("https://")
        assert resolve_encryption_key(url, "OD4B3F63") == KEY.hex()

    def test_non_od_name_skips_device_check(self) -> None:
        """A renamed entry title can't be compared, so the check is skipped."""
        assert resolve_encryption_key(landing_url(), "Living Room Tag") == KEY.hex()

    def test_wrong_device(self) -> None:
        """A QR code of another OD device is rejected."""
        with pytest.raises(QrCodeWrongDevice):
            resolve_encryption_key(landing_url(b"\x01\x02\x03"), "OD4B3F63")

    def test_key_hidden(self) -> None:
        """An all-zero key slot means the device hides its key."""
        with pytest.raises(QrCodeKeyHidden):
            resolve_encryption_key(landing_url(key=None), "OD4B3F63")

    @pytest.mark.parametrize(
        "value",
        [
            "tooshort",
            "zz" * 16,
            "https://example.com/l/?AAAA",
            "https://opendisplay.org/l/?AAAA",
            "https://opendisplay.org/l/?",
        ],
    )
    def test_invalid(self, value: str) -> None:
        """Malformed keys and URLs raise InvalidKeyFormat."""
        with pytest.raises(InvalidKeyFormat):
            resolve_encryption_key(value, "OD4B3F63")
