"""Unit tests for OpenDisplay persisted content encoding."""

from opendisplay import PartialState, RefreshMode
from PIL import Image

from custom_components.opendisplay.storage import (
    StoredContent,
    StoredPendingUpload,
    _decode_content,
    _decode_pending_upload,
    _encode_content,
    _encode_pending_upload,
)


def test_pending_upload_round_trips_prepared_image_and_partial_state():
    """Persisted uploads keep enough data to resume without re-rendering."""
    processed = Image.new("P", (2, 2))
    partial = PartialState(
        etag=123,
        last_image=b"\x00\x01\x02\x03",
        width=2,
        height=2,
        bytes_per_pixel=1,
    )
    upload = StoredPendingUpload(
        prepared=(b"raw", b"zip", processed),
        refresh_mode=RefreshMode.PARTIAL,
        partial_state=partial,
        use_measured_palettes=True,
        preview_jpeg=b"jpeg",
        device_id="device-id",
        queued_at=1000.0,
        expires_at=2000.0,
        attempts=3,
        paused=True,
    )

    restored = _decode_pending_upload(_encode_pending_upload(upload))

    assert restored is not None
    assert restored.prepared[0] == b"raw"
    assert restored.prepared[1] == b"zip"
    assert restored.prepared[2].mode == "P"
    assert restored.prepared[2].size == (2, 2)
    assert restored.refresh_mode is RefreshMode.PARTIAL
    assert restored.partial_state == partial
    assert restored.use_measured_palettes is True
    assert restored.preview_jpeg == b"jpeg"
    assert restored.device_id == "device-id"
    assert restored.queued_at == 1000.0
    assert restored.expires_at == 2000.0
    assert restored.attempts == 3
    assert restored.paused is True


def test_content_round_trips_image_entity_attributes():
    content = StoredContent(
        image_jpeg=b"jpeg",
        image_last_updated=1000.0,
        pending=True,
        queued_at=1000.0,
        expires_at=2000.0,
        attempts=2,
        last_error="boom",
    )

    restored = _decode_content(_encode_content(content))

    assert restored == content
