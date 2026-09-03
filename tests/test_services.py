"""Test the OpenDisplay upload_image service."""

import asyncio
from collections.abc import Generator
from dataclasses import replace
import io
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from opendisplay import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    ColorScheme,
    NfcNotSupportedError,
    NfcWriteError,
)
from PIL import Image as PILImage
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
import voluptuous as vol

from custom_components.opendisplay.const import CONF_ENCRYPTION_KEY, DOMAIN
from custom_components.opendisplay.services import HA_TAG_URL_PREFIX, NFC_MAX_PAYLOAD

from . import ENCRYPTION_KEY, make_nfc_device_config, make_notifier_device_config


@pytest.fixture(autouse=True)
async def setup_entry(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Set up the config entry for service tests."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def mock_upload_device(mock_opendisplay_device: MagicMock) -> MagicMock:
    """Return the mock OpenDisplayDevice for upload service tests."""
    return mock_opendisplay_device


@pytest.fixture
def mock_resolve_media(tmp_path: Path) -> Generator[MagicMock]:
    """Mock async_resolve_media to return a local test image."""
    image_path = tmp_path / "test.png"
    PILImage.new("RGB", (10, 10)).save(image_path)
    mock_media = MagicMock()
    mock_media.path = image_path
    with patch(
        "custom_components.opendisplay.services.async_resolve_media",
        return_value=mock_media,
    ):
        yield mock_media


def _device_id(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> str:
    """Return the device registry ID for the config entry."""
    registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    assert devices
    return devices[0].id


async def test_upload_image_local_file(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_upload_device: MagicMock,
    mock_resolve_media: MagicMock,
) -> None:
    """Test successful upload from a local file with tone compression."""
    device_id = _device_id(hass, mock_config_entry)

    await hass.services.async_call(
        DOMAIN,
        "upload_image",
        {
            "device_id": device_id,
            "image": {
                "media_content_id": "media-source://local/test.png",
                "media_content_type": "image/png",
            },
            "tone_compression": 50,
        },
        blocking=True,
    )

    mock_upload_device.upload_prepared_image.assert_called_once()


async def test_upload_image_refreshes_runtime_config(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_upload_device: MagicMock,
    mock_resolve_media: MagicMock,
) -> None:
    """A live upload re-interrogates the device and refreshes the cached config.

    This is what catches a config change made without a device reboot — the
    only other event that would otherwise trigger a refresh.
    """
    device_id = _device_id(hass, mock_config_entry)

    await hass.services.async_call(
        DOMAIN,
        "upload_image",
        {
            "device_id": device_id,
            "image": {
                "media_content_id": "media-source://local/test.png",
                "media_content_type": "image/png",
            },
        },
        blocking=True,
    )

    mock_upload_device.interrogate.assert_awaited()
    assert mock_config_entry.runtime_data.device_config is mock_upload_device.config


async def test_upload_image_config_mismatch_rerenders_and_uploads(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_upload_device: MagicMock,
    mock_resolve_media: MagicMock,
) -> None:
    """A config drift detected mid-connection re-renders instead of sending stale bytes.

    Unlike the queued/sleepy path, the original source image is still in
    scope for a live upload, so the corrected frame can be sent transparently
    -- the service call must still succeed, not raise.
    """
    device_id = _device_id(hass, mock_config_entry)
    original_config = mock_upload_device.config
    mismatched_config = replace(
        original_config,
        displays=[
            replace(original_config.displays[0], color_scheme=ColorScheme.MONO.value)
        ],
    )

    def _drift_on_interrogate() -> None:
        mock_upload_device.config = mismatched_config

    mock_upload_device.interrogate.side_effect = _drift_on_interrogate

    from custom_components.opendisplay import services as services_mod

    real_prepare_image = services_mod.prepare_image
    prepared_results: list[Any] = []

    def _tracking_prepare_image(*args: Any, **kwargs: Any) -> Any:
        result = real_prepare_image(*args, **kwargs)
        prepared_results.append(result)
        return result

    with patch.object(
        services_mod, "prepare_image", side_effect=_tracking_prepare_image
    ) as spy_prepare:
        await hass.services.async_call(
            DOMAIN,
            "upload_image",
            {
                "device_id": device_id,
                "image": {
                    "media_content_id": "media-source://local/test.png",
                    "media_content_type": "image/png",
                },
            },
            blocking=True,
        )

    assert spy_prepare.call_count == 2
    assert spy_prepare.call_args_list[0].kwargs["config"] is original_config
    assert spy_prepare.call_args_list[1].kwargs["config"] is mismatched_config
    # Different color schemes must not encode to the same bytes.
    assert prepared_results[0] != prepared_results[1]
    mock_upload_device.upload_prepared_image.assert_called_once()
    sent_data = mock_upload_device.upload_prepared_image.call_args[0][0]
    assert sent_data == prepared_results[1]


async def test_upload_image_remote_url(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_upload_device: MagicMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test successful upload from a remote URL."""
    device_id = _device_id(hass, mock_config_entry)

    image = PILImage.new("RGB", (10, 10))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    aioclient_mock.get("http://example.com/image.png", content=buf.getvalue())

    mock_media = MagicMock()
    mock_media.path = None
    mock_media.url = "http://example.com/image.png"

    with patch(
        "custom_components.opendisplay.services.async_resolve_media",
        return_value=mock_media,
    ):
        await hass.services.async_call(
            DOMAIN,
            "upload_image",
            {
                "device_id": device_id,
                "image": {
                    "media_content_id": "media-source://local/test.png",
                    "media_content_type": "image/png",
                },
            },
            blocking=True,
        )

    mock_upload_device.upload_prepared_image.assert_called_once()


async def test_upload_image_invalid_device_id(
    hass: HomeAssistant,
) -> None:
    """Test that an invalid device_id raises ServiceValidationError."""
    with pytest.raises(ServiceValidationError, match="not a valid OpenDisplay device"):
        await hass.services.async_call(
            DOMAIN,
            "upload_image",
            {
                "device_id": "not-a-real-device-id",
                "image": {
                    "media_content_id": "media-source://local/test.png",
                    "media_content_type": "image/png",
                },
            },
            blocking=True,
        )


async def test_upload_image_device_not_in_range(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that HomeAssistantError is raised if device is out of BLE range."""
    device_id = _device_id(hass, mock_config_entry)

    with (
        patch(
            "custom_components.opendisplay.transport.async_ble_device_from_address",
            return_value=None,
        ),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            DOMAIN,
            "upload_image",
            {
                "device_id": device_id,
                "image": {
                    "media_content_id": "media-source://local/test.png",
                    "media_content_type": "image/png",
                },
            },
            blocking=True,
        )


async def test_upload_image_ble_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_opendisplay_device: MagicMock,
    mock_resolve_media: MagicMock,
) -> None:
    """Test that HomeAssistantError is raised on BLE upload failure."""
    device_id = _device_id(hass, mock_config_entry)

    mock_opendisplay_device.__aenter__.side_effect = BLEConnectionError(
        "connection lost"
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "upload_image",
            {
                "device_id": device_id,
                "image": {
                    "media_content_id": "media-source://local/test.png",
                    "media_content_type": "image/png",
                },
            },
            blocking=True,
        )


@pytest.mark.parametrize(
    "exc",
    [
        aiohttp.ClientError("connection refused"),
        TimeoutError(),
    ],
)
async def test_upload_image_download_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    exc: Exception,
) -> None:
    """Test that HomeAssistantError is raised on media download failure."""
    device_id = _device_id(hass, mock_config_entry)

    aioclient_mock.get("http://example.com/image.png", exc=exc)

    mock_media = MagicMock()
    mock_media.path = None
    mock_media.url = "http://example.com/image.png"

    with (
        patch(
            "custom_components.opendisplay.services.async_resolve_media",
            return_value=mock_media,
        ),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            DOMAIN,
            "upload_image",
            {
                "device_id": device_id,
                "image": {
                    "media_content_id": "media-source://local/test.png",
                    "media_content_type": "image/png",
                },
            },
            blocking=True,
        )


def _png_bytes() -> bytes:
    """Return a small PNG as raw bytes."""
    buf = io.BytesIO()
    PILImage.new("RGB", (10, 10)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize(
    ("domain", "entity_id", "path"),
    [
        ("camera", "camera.front_door", "/api/camera_proxy/camera.front_door"),
        ("image", "image.tag_content", "/api/image_proxy/image.tag_content"),
    ],
)
async def test_upload_image_entity_still_frame(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_upload_device: MagicMock,
    aioclient_mock: AiohttpClientMocker,
    domain: str,
    entity_id: str,
    path: str,
) -> None:
    """Camera/image entities are fetched from their still endpoint."""
    device_id = _device_id(hass, mock_config_entry)
    await async_process_ha_core_config(hass, {"internal_url": "http://127.0.0.1:8123"})
    aioclient_mock.get(f"http://127.0.0.1:8123{path}", content=_png_bytes())

    with patch(
        "custom_components.opendisplay.services.async_resolve_media",
    ) as mock_resolve:
        await hass.services.async_call(
            DOMAIN,
            "upload_image",
            {
                "device_id": device_id,
                "image": {
                    "media_content_id": f"media-source://{domain}/{entity_id}",
                    "media_content_type": "image/jpeg",
                },
            },
            blocking=True,
        )

    assert aioclient_mock.call_count == 1
    assert aioclient_mock.mock_calls[0][1].path == path
    mock_resolve.assert_not_called()
    mock_upload_device.upload_prepared_image.assert_called_once()


@pytest.mark.parametrize(
    "field",
    ["dither_mode", "fit_mode", "refresh_mode"],
)
async def test_upload_image_invalid_mode(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    field: str,
) -> None:
    """Test that invalid mode strings are rejected by the schema."""
    device_id = _device_id(hass, mock_config_entry)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "upload_image",
            {
                "device_id": device_id,
                "image": {
                    "media_content_id": "media-source://local/test.png",
                    "media_content_type": "image/png",
                },
                field: "not_a_valid_value",
            },
            blocking=True,
        )


async def test_upload_image_cancels_previous_task(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_upload_device: MagicMock,
    mock_resolve_media: MagicMock,
) -> None:
    """Test that starting a new upload cancels an in-progress upload task."""
    device_id = _device_id(hass, mock_config_entry)

    prev_task = hass.async_create_task(asyncio.sleep(3600))
    mock_config_entry.runtime_data.upload_task = prev_task

    await hass.services.async_call(
        DOMAIN,
        "upload_image",
        {
            "device_id": device_id,
            "image": {
                "media_content_id": "media-source://local/test.png",
                "media_content_type": "image/png",
            },
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert prev_task.cancelled()


async def test_upload_image_with_encryption_key(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_opendisplay_device_class: MagicMock,
    mock_resolve_media: MagicMock,
) -> None:
    """Test that upload_image passes the encryption key to OpenDisplayDevice."""
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_ENCRYPTION_KEY: ENCRYPTION_KEY},
    )

    device_id = _device_id(hass, mock_config_entry)

    await hass.services.async_call(
        DOMAIN,
        "upload_image",
        {
            "device_id": device_id,
            "image": {
                "media_content_id": "media-source://local/test.png",
                "media_content_type": "image/png",
            },
        },
        blocking=True,
    )

    assert mock_opendisplay_device_class.call_args.kwargs[
        "encryption_key"
    ] == bytes.fromhex(ENCRYPTION_KEY)


@pytest.mark.parametrize(
    "exception",
    [
        AuthenticationFailedError("wrong key"),
        AuthenticationRequiredError("auth required"),
    ],
)
async def test_upload_image_auth_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_opendisplay_device: MagicMock,
    mock_resolve_media: MagicMock,
    exception: Exception,
) -> None:
    """Test that auth errors during upload trigger a reauth flow."""
    device_id = _device_id(hass, mock_config_entry)

    mock_opendisplay_device.__aenter__.side_effect = exception

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "upload_image",
            {
                "device_id": device_id,
                "image": {
                    "media_content_id": "media-source://local/test.png",
                    "media_content_type": "image/png",
                },
            },
            blocking=True,
        )

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == config_entries.SOURCE_REAUTH for f in flows)


async def test_upload_image_invalid_encryption_key_format(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_resolve_media: MagicMock,
) -> None:
    """Test malformed encryption key triggers reauth and error."""
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_ENCRYPTION_KEY: "not-valid-hex!"},
    )
    device_id = _device_id(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "upload_image",
            {
                "device_id": device_id,
                "image": {
                    "media_content_id": "media-source://local/test.png",
                    "media_content_type": "image/png",
                },
            },
            blocking=True,
        )

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == config_entries.SOURCE_REAUTH for f in flows)


# --- write_nfc -------------------------------------------------------------


@pytest.fixture
def mock_nfc_write(mock_opendisplay_device: MagicMock) -> Generator[MagicMock]:
    """Run the service's BLE body against the mocked device, without connecting."""

    async def _run(hass, entry, action):
        return await action(mock_opendisplay_device)

    with patch("custom_components.opendisplay.services._async_connect_and_run", _run):
        yield mock_opendisplay_device


async def _write_nfc(hass: HomeAssistant, device_id: str, **data) -> None:
    """Call write_nfc through the service registry."""
    await hass.services.async_call(
        DOMAIN, "write_nfc", {"device_id": device_id, **data}, blocking=True
    )


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
async def test_write_nfc_url_is_the_default_record_type(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """Omitting record_type writes a URL record."""
    await _write_nfc(
        hass, _device_id(hass, mock_config_entry), content="http://a.test/"
    )

    mock_nfc_write.write_nfc_url.assert_awaited_once_with("http://a.test/")


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
async def test_write_nfc_text_record(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """A text record goes to write_nfc_text."""
    await _write_nfc(
        hass, _device_id(hass, mock_config_entry), content="hello", record_type="text"
    )

    mock_nfc_write.write_nfc_text.assert_awaited_once_with("hello")


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
async def test_write_nfc_ha_tag_composes_and_quotes_the_url(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """A ha_tag id becomes a home-assistant.io/tag URL with the id percent-encoded."""
    await _write_nfc(
        hass,
        _device_id(hass, mock_config_entry),
        content="tag/with spaces&stuff",
        record_type="ha_tag",
    )

    mock_nfc_write.write_nfc_url.assert_awaited_once_with(
        HA_TAG_URL_PREFIX + "tag%2Fwith%20spaces%26stuff"
    )


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
async def test_write_nfc_mime_defaults_to_vcard(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """A mime record with no mime_type defaults to text/vcard."""
    await _write_nfc(
        hass,
        _device_id(hass, mock_config_entry),
        content="BEGIN:VCARD",
        record_type="mime",
    )

    assert mock_nfc_write.write_nfc_mime.await_args.args[0] == "text/vcard"


@pytest.mark.parametrize(
    "device_config", [make_nfc_device_config(enabled=False)], ids=["nfc-disabled"]
)
async def test_write_nfc_rejected_when_the_tag_has_no_enabled_nfc(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """A device whose NFC is present but not enabled cannot be written to."""
    with pytest.raises(ServiceValidationError) as err:
        await _write_nfc(
            hass, _device_id(hass, mock_config_entry), content="http://a.test/"
        )

    assert err.value.translation_key == "no_nfc"
    mock_nfc_write.write_nfc_url.assert_not_awaited()


async def test_write_nfc_rejected_when_the_tag_has_no_nfc_at_all(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """The default device config has no NFC hardware."""
    with pytest.raises(ServiceValidationError) as err:
        await _write_nfc(
            hass, _device_id(hass, mock_config_entry), content="http://a.test/"
        )

    assert err.value.translation_key == "no_nfc"


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
async def test_write_nfc_measures_the_payload_in_bytes_not_characters(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """A multibyte string over the limit is rejected even though it is fewer chars."""
    device_id = _device_id(hass, mock_config_entry)
    # 3 bytes per character in UTF-8, so this is over NFC_MAX_PAYLOAD bytes
    # while being only a third as many characters.
    oversized = "中" * (NFC_MAX_PAYLOAD // 3 + 1)

    with pytest.raises(ServiceValidationError) as err:
        await _write_nfc(hass, device_id, content=oversized, record_type="text")

    assert err.value.translation_key == "nfc_content_too_long"


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
async def test_write_nfc_accepts_content_at_exactly_the_limit(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """The limit is inclusive."""
    await _write_nfc(
        hass,
        _device_id(hass, mock_config_entry),
        content="a" * NFC_MAX_PAYLOAD,
        record_type="text",
    )

    mock_nfc_write.write_nfc_text.assert_awaited_once()


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (NfcNotSupportedError("nope"), "nfc_not_supported"),
        (NfcWriteError("bad"), "nfc_write_failed"),
    ],
)
async def test_write_nfc_translates_library_errors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
    error: Exception,
    translation_key: str,
) -> None:
    """Library failures surface as HomeAssistantError with a translated message."""
    mock_nfc_write.write_nfc_url.side_effect = error

    with pytest.raises(HomeAssistantError) as err:
        await _write_nfc(
            hass, _device_id(hass, mock_config_entry), content="http://a.test/"
        )

    assert err.value.translation_key == translation_key


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
async def test_write_nfc_rejects_an_unknown_record_type(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """The service schema constrains record_type."""
    with pytest.raises(vol.Invalid):
        await _write_nfc(
            hass,
            _device_id(hass, mock_config_entry),
            content="x",
            record_type="not-a-type",
        )


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
async def test_write_nfc_mime_honours_an_explicit_mime_type(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """An explicit mime_type overrides the vcard default."""
    await _write_nfc(
        hass,
        _device_id(hass, mock_config_entry),
        content="a,b",
        record_type="mime",
        mime_type="text/csv",
    )

    assert mock_nfc_write.write_nfc_mime.await_args.args[0] == "text/csv"


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
async def test_write_nfc_ha_tag_leaves_a_plain_id_alone(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """An id with nothing to escape is appended unchanged."""
    await _write_nfc(
        hass,
        _device_id(hass, mock_config_entry),
        content="abc123",
        record_type="ha_tag",
    )

    mock_nfc_write.write_nfc_url.assert_awaited_once_with(HA_TAG_URL_PREFIX + "abc123")


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
async def test_write_nfc_rejects_a_mime_type_on_a_url_record(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """mime_type only means something for a mime record."""
    with pytest.raises(ServiceValidationError) as err:
        await _write_nfc(
            hass,
            _device_id(hass, mock_config_entry),
            content="http://a.test/",
            mime_type="text/csv",
        )

    assert err.value.translation_key == "mime_type_not_applicable"


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
@pytest.mark.parametrize("record_type", ["text", "ha_tag"])
async def test_write_nfc_rejects_empty_content(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
    record_type: str,
) -> None:
    """There is nothing to write, for a plain record or a tag id."""
    with pytest.raises(ServiceValidationError) as err:
        await _write_nfc(
            hass,
            _device_id(hass, mock_config_entry),
            content="",
            record_type=record_type,
        )

    assert err.value.translation_key == "nfc_content_empty"


@pytest.mark.parametrize("device_config", [make_nfc_device_config()])
async def test_write_nfc_counts_the_mime_header_against_the_limit(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """A body that fits alone can still overflow once the mime type is prepended."""
    mime_type = "application/vnd.test"
    body = "a" * (NFC_MAX_PAYLOAD - len(mime_type) + 1)

    with pytest.raises(ServiceValidationError) as err:
        await _write_nfc(
            hass,
            _device_id(hass, mock_config_entry),
            content=body,
            record_type="mime",
            mime_type=mime_type,
        )

    assert err.value.translation_key == "nfc_content_too_long"


@pytest.mark.parametrize("device_config", [make_nfc_device_config(sleepy=True)])
async def test_write_nfc_rejected_while_the_device_sleeps(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_nfc_write: MagicMock,
) -> None:
    """NFC needs a live connection, so it cannot be queued for the next wake.

    Unlike an image, there is nothing useful to do with a deferred NFC write.
    """
    with pytest.raises(HomeAssistantError) as err:
        await _write_nfc(
            hass, _device_id(hass, mock_config_entry), content="http://a.test/"
        )

    assert err.value.translation_key == "device_sleeping"


# --- activate_led / activate_buzzer / play_melody ---------------------------


@pytest.fixture
def mock_connect(mock_opendisplay_device: MagicMock) -> Generator[MagicMock]:
    """Run a service's BLE body against the mocked device, without connecting."""

    async def _run(hass, entry, action):
        return await action(mock_opendisplay_device)

    with patch("custom_components.opendisplay.services._async_connect_and_run", _run):
        yield mock_opendisplay_device


async def _call(hass: HomeAssistant, service: str, device_id: str, **data) -> None:
    await hass.services.async_call(
        DOMAIN, service, {"device_id": device_id, **data}, blocking=True
    )


@pytest.mark.parametrize("device_config", [make_notifier_device_config()])
async def test_activate_led(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connect: MagicMock,
) -> None:
    """The LED flash configuration reaches the device."""
    await _call(hass, "activate_led", _device_id(hass, mock_config_entry), brightness=5)

    instance, flash_config = mock_connect.activate_led.await_args.args
    assert instance == 0
    assert flash_config.brightness == 5


@pytest.mark.parametrize("device_config", [make_notifier_device_config()])
async def test_activate_buzzer(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connect: MagicMock,
) -> None:
    """A single tone reaches the device with its frequency and duration."""
    await _call(
        hass,
        "activate_buzzer",
        _device_id(hass, mock_config_entry),
        frequency_hz=440,
        duration_ms=250,
    )

    mock_connect.activate_buzzer.assert_awaited_once()


@pytest.mark.parametrize("device_config", [make_notifier_device_config()])
async def test_play_melody(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connect: MagicMock,
) -> None:
    """A melody string is compiled and sent to the buzzer."""
    await _call(
        hass, "play_melody", _device_id(hass, mock_config_entry), notes="C4 D4 E4 F4"
    )

    mock_connect.activate_buzzer.assert_awaited_once()


@pytest.mark.parametrize("device_config", [make_notifier_device_config()])
async def test_play_melody_rejects_an_unplayable_melody(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connect: MagicMock,
) -> None:
    """A note too long for the firmware at the chosen tempo is a user error.

    The schema validates note syntax at the default tempo, so tempo-dependent
    overflow can only be caught by the handler: a quarter note is 500 ms at
    tempo 120 but 1500 ms at tempo 40, past the firmware's 1275 ms ceiling.
    """
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "play_melody",
            _device_id(hass, mock_config_entry),
            notes="C4",
            tempo=40,
            default_length=4,
        )

    assert err.value.translation_key == "invalid_melody"


@pytest.mark.parametrize(
    ("service", "device_config", "translation_key", "extra"),
    [
        (
            "activate_led",
            make_notifier_device_config(leds=False),
            "no_leds",
            {},
        ),
        (
            "activate_buzzer",
            make_notifier_device_config(buzzers=False),
            "no_buzzers",
            {},
        ),
        (
            "play_melody",
            make_notifier_device_config(buzzers=False),
            "no_buzzers",
            {"notes": "C4"},
        ),
    ],
    ids=["led", "buzzer", "melody"],
)
async def test_notifier_services_need_the_hardware(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connect: MagicMock,
    service: str,
    translation_key: str,
    extra: dict,
) -> None:
    """A device without the hardware rejects the call instead of connecting."""
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, service, _device_id(hass, mock_config_entry), **extra)

    assert err.value.translation_key == translation_key
    mock_connect.activate_led.assert_not_awaited()
    mock_connect.activate_buzzer.assert_not_awaited()


@pytest.mark.parametrize("device_config", [make_notifier_device_config(sleepy=True)])
async def test_notifications_are_refused_while_the_device_sleeps(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connect: MagicMock,
) -> None:
    """An LED flash that fires hours late is worse than an error.

    Unlike an image, a notification cannot usefully be queued for the next
    wake, so a sleeping tag rejects it outright.
    """
    with pytest.raises(HomeAssistantError):
        await _call(hass, "activate_led", _device_id(hass, mock_config_entry))

    mock_connect.activate_led.assert_not_awaited()


# --- drawcustom -------------------------------------------------------------


@pytest.fixture
def mock_render() -> Generator[MagicMock]:
    """Render a real 1x1 image without invoking the drawing engine."""
    with patch(
        "custom_components.opendisplay.services.generate_image",
        AsyncMock(return_value=PILImage.new("RGB", (10, 10))),
    ) as render:
        yield render


async def test_drawcustom_renders_and_uploads(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_upload_device: MagicMock,
    mock_render: MagicMock,
) -> None:
    """A payload is rendered at the panel's size and sent to the device."""
    response = await hass.services.async_call(
        DOMAIN,
        "drawcustom",
        {
            "device_id": [_device_id(hass, mock_config_entry)],
            "payload": [{"type": "text", "value": "hi"}],
        },
        blocking=True,
        return_response=True,
    )

    assert mock_render.await_args.kwargs["width"] == 296
    assert mock_render.await_args.kwargs["height"] == 128
    mock_upload_device.upload_prepared_image.assert_awaited_once()
    assert response["status"] == "delivered"


async def test_drawcustom_dry_run_previews_without_uploading(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_upload_device: MagicMock,
    mock_render: MagicMock,
) -> None:
    """A dry run shows what would be sent but never touches the panel."""
    response = await hass.services.async_call(
        DOMAIN,
        "drawcustom",
        {
            "device_id": [_device_id(hass, mock_config_entry)],
            "payload": [{"type": "text", "value": "hi"}],
            "dry-run": True,
        },
        blocking=True,
        return_response=True,
    )

    assert response["status"] == "dry_run"
    mock_upload_device.upload_prepared_image.assert_not_awaited()


async def test_drawcustom_needs_a_target(
    hass: HomeAssistant, mock_render: MagicMock
) -> None:
    """A call naming no device, area or label has nothing to draw on."""
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "drawcustom",
            {"payload": [{"type": "text", "value": "hi"}]},
            blocking=True,
            return_response=True,
        )

    assert err.value.translation_key == "no_targets_specified"


async def test_drawcustom_reports_every_failed_target(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_render: MagicMock,
) -> None:
    """One bad device id does not silently disappear from a multi-target call.

    Failures are collected across targets and raised together, so a batch
    reports each device that could not be drawn rather than the first only.
    """
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "drawcustom",
            {
                "device_id": [_device_id(hass, mock_config_entry), "not-a-device"],
                "payload": [{"type": "text", "value": "hi"}],
            },
            blocking=True,
            return_response=True,
        )

    assert err.value.translation_key == "multiple_errors"
    assert "not-a-device" in err.value.translation_placeholders["errors"]


async def test_drawcustom_transposes_the_canvas_for_a_rotated_panel(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_upload_device: MagicMock,
    mock_render: MagicMock,
) -> None:
    """A 90-degree rotation renders transposed so the device fit stays 1:1.

    The payload is authored against the final on-screen orientation, and the
    device applies the rotation itself; rendering portrait-shaped content onto
    a landscape canvas would make the device scale and centre a mismatched
    image instead.
    """
    await hass.services.async_call(
        DOMAIN,
        "drawcustom",
        {
            "device_id": [_device_id(hass, mock_config_entry)],
            "payload": [{"type": "text", "value": "hi"}],
            "rotate": 90,
        },
        blocking=True,
        return_response=True,
    )

    assert mock_render.await_args.kwargs["width"] == 128
    assert mock_render.await_args.kwargs["height"] == 296
