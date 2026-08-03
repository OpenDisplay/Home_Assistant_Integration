"""Persistent content storage for OpenDisplay entries."""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass, replace
from io import BytesIO
import time
from typing import Any

from opendisplay import PartialState, RefreshMode
from PIL import Image as PILImage

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1
# Always-on devices usually drain a queued image quickly; delaying the save avoids
# persisting transient queue state that is cleared almost immediately. Sleeping
# devices keep the queue long enough that the delayed write makes it durable.
CONTENT_IMAGE_SAVE_DELAY = 60.0
# Clearing an already-persisted queue slot is cheap and should land promptly so a
# crash does not resurrect already-delivered content on the next startup.
CONTENT_EMPTY_SAVE_DELAY = 1.0


@dataclass(frozen=True)
class StoredContent:
    """Last image shown by the content image entity."""

    image_jpeg: bytes | None = None
    image_last_updated: float | None = None
    pending: bool = False
    queued_at: float | None = None
    expires_at: float | None = None
    attempts: int = 0
    last_error: str | None = None


@dataclass(frozen=True)
class StoredPendingUpload:
    """Queued upload state that can be restored after HA restarts."""

    prepared: tuple[bytes, bytes | None, PILImage.Image]
    refresh_mode: RefreshMode
    partial_state: PartialState
    use_measured_palettes: bool
    preview_jpeg: bytes
    device_id: str | None
    queued_at: float
    expires_at: float
    attempts: int = 0
    paused: bool = False


class OpenDisplayContentStore:
    """Versioned Store wrapper for the entry's content and queued upload."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the store."""
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{entry_id}.content",
            private=True,
            atomic_writes=True,
            serialize_in_event_loop=False,
        )
        self.content = StoredContent()
        self.pending_upload: StoredPendingUpload | None = None

    async def async_load(self) -> None:
        """Load persisted content and drop an expired pending upload."""
        raw = await self._store.async_load()
        if not raw:
            return

        self.content = _decode_content(raw.get("content"))
        self.pending_upload = _decode_pending_upload(raw.get("pending_upload"))

        now = time.time()
        if self.pending_upload is not None and self.pending_upload.expires_at <= now:
            self.pending_upload = None
            self.content = replace(
                self.content,
                pending=False,
                queued_at=None,
                expires_at=None,
                attempts=0,
                last_error="expired",
            )
            await self._store.async_save(self._serialize())

    @callback
    def store_content(
        self,
        image_jpeg: bytes,
        *,
        image_last_updated: float,
        pending: bool,
        queued_at: float | None,
        expires_at: float | None,
        attempts: int,
        last_error: str | None,
    ) -> None:
        """Persist the image entity's current content state."""
        content = StoredContent(
            image_jpeg=image_jpeg,
            image_last_updated=image_last_updated,
            pending=pending,
            queued_at=queued_at,
            expires_at=expires_at,
            attempts=attempts,
            last_error=last_error,
        )
        if content == self.content:
            return
        self.content = content
        self._schedule_save()

    @callback
    def store_pending_upload(self, upload: StoredPendingUpload) -> None:
        """Persist a queued upload."""
        if upload == self.pending_upload:
            return
        self.pending_upload = upload
        self._schedule_save()

    @callback
    def clear_pending_upload(self, *, last_error: str | None = None) -> None:
        """Clear the persisted upload slot and mark content no longer pending."""
        self.pending_upload = None
        self.content = replace(
            self.content,
            pending=False,
            queued_at=None,
            expires_at=None,
            attempts=0,
            last_error=last_error,
        )
        self._schedule_save(CONTENT_EMPTY_SAVE_DELAY)

    @callback
    def update_pending_snapshot(
        self,
        *,
        pending: bool,
        queued_at: float | None,
        expires_at: float | None,
        attempts: int,
        last_error: str | None,
    ) -> None:
        """Persist queue attributes without changing the stored image bytes."""
        content = replace(
            self.content,
            pending=pending,
            queued_at=queued_at,
            expires_at=expires_at,
            attempts=attempts,
            last_error=last_error,
        )
        if content == self.content:
            return
        self.content = content
        self._schedule_save()

    @callback
    def _schedule_save(self, delay: float = CONTENT_IMAGE_SAVE_DELAY) -> None:
        """Schedule a storage write for the current state."""
        self._store.async_delay_save(self._serialize, delay)

    def _serialize(self) -> dict[str, Any]:
        """Serialize the current store state."""
        return {
            "content": _encode_content(self.content),
            "pending_upload": _encode_pending_upload(self.pending_upload),
        }


def _b64(data: bytes | None) -> str | None:
    """Encode bytes to a JSON string."""
    return b64encode(data).decode("ascii") if data is not None else None


def _unb64(data: str | None) -> bytes | None:
    """Decode bytes from a JSON string."""
    return b64decode(data.encode("ascii")) if data is not None else None


def _image_to_png(image: PILImage.Image) -> str:
    """Serialize a processed image losslessly."""
    buf = BytesIO()
    image.save(buf, format="PNG")
    return _b64(buf.getvalue()) or ""


def _image_from_png(data: str) -> PILImage.Image:
    """Load a processed image from storage."""
    image = PILImage.open(BytesIO(b64decode(data.encode("ascii"))))
    image.load()
    return image


def _encode_content(content: StoredContent) -> dict[str, Any]:
    """Encode StoredContent to JSON-compatible data."""
    return {
        "image_jpeg": _b64(content.image_jpeg),
        "image_last_updated": content.image_last_updated,
        "pending": content.pending,
        "queued_at": content.queued_at,
        "expires_at": content.expires_at,
        "attempts": content.attempts,
        "last_error": content.last_error,
    }


def _decode_content(raw: Any) -> StoredContent:
    """Decode StoredContent from storage."""
    if not isinstance(raw, dict):
        return StoredContent()
    try:
        return StoredContent(
            image_jpeg=_unb64(raw.get("image_jpeg")),
            image_last_updated=raw.get("image_last_updated"),
            pending=bool(raw.get("pending", False)),
            queued_at=raw.get("queued_at"),
            expires_at=raw.get("expires_at"),
            attempts=int(raw.get("attempts", 0)),
            last_error=raw.get("last_error"),
        )
    except (TypeError, ValueError):
        return StoredContent()


def _encode_pending_upload(upload: StoredPendingUpload | None) -> dict[str, Any] | None:
    """Encode a queued upload to JSON-compatible data."""
    if upload is None:
        return None
    uncompressed, compressed, processed = upload.prepared
    return {
        "uncompressed": _b64(uncompressed),
        "compressed": _b64(compressed),
        "processed_png": _image_to_png(processed),
        "refresh_mode": int(upload.refresh_mode),
        "partial_state": _b64(upload.partial_state.to_bytes()),
        "use_measured_palettes": upload.use_measured_palettes,
        "preview_jpeg": _b64(upload.preview_jpeg),
        "device_id": upload.device_id,
        "queued_at": upload.queued_at,
        "expires_at": upload.expires_at,
        "attempts": upload.attempts,
        "paused": upload.paused,
    }


def _decode_pending_upload(raw: Any) -> StoredPendingUpload | None:
    """Decode a queued upload from storage."""
    if not isinstance(raw, dict):
        return None
    try:
        uncompressed = _unb64(raw.get("uncompressed"))
        preview_jpeg = _unb64(raw.get("preview_jpeg"))
        partial_state = _unb64(raw.get("partial_state"))
        if uncompressed is None or preview_jpeg is None or partial_state is None:
            return None
        return StoredPendingUpload(
            prepared=(
                uncompressed,
                _unb64(raw.get("compressed")),
                _image_from_png(raw["processed_png"]),
            ),
            refresh_mode=RefreshMode(raw["refresh_mode"]),
            partial_state=PartialState.from_bytes(partial_state),
            use_measured_palettes=bool(raw["use_measured_palettes"]),
            preview_jpeg=preview_jpeg,
            device_id=raw.get("device_id"),
            queued_at=float(raw["queued_at"]),
            expires_at=float(raw["expires_at"]),
            attempts=int(raw.get("attempts", 0)),
            paused=bool(raw.get("paused", False)),
        )
    except (KeyError, TypeError, ValueError):
        return None
