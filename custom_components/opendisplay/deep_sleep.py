"""Deep-sleep upload queue data structures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from opendisplay import OpenDisplayDevice


@dataclass
class DeepSleepQueuedUpload:
    """A pending upload waiting for a sleeping device to wake up."""

    action: Callable[["OpenDisplayDevice"], Awaitable[None]]
    jpeg_bytes: bytes
    queued_at: datetime
    expiry: timedelta

    @property
    def is_expired(self) -> bool:
        """Return True if the upload has passed its expiry window."""
        return (datetime.now() - self.queued_at) > self.expiry
