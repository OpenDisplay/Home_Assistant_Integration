"""Test the OpenDisplay image platform."""

from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.opendisplay.const import (
    SIGNAL_IMAGE_UPDATED,
    SIGNAL_PENDING_STATE,
)
from custom_components.opendisplay.delivery import DeliverySnapshot

from . import TEST_ADDRESS

ENTITY = "image.opendisplay_1234_display_content"
JPEG = b"\xff\xd8\xff\xe0 not really a jpeg"


@pytest.fixture
def platforms() -> list[Platform]:
    """Only set up the image platform."""
    return [Platform.IMAGE]


def _send_image(hass: HomeAssistant, image: bytes = JPEG) -> None:
    """Emit the signal a completed or queued upload sends."""
    async_dispatcher_send(hass, f"{SIGNAL_IMAGE_UPDATED}_{TEST_ADDRESS}", image)


async def test_entity_is_created(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """The image entity exists before anything has been sent."""
    await setup_entry()

    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.attributes["pending"] is False
    assert state.attributes["queued_at"] is None
    assert state.attributes["auth_paused"] is False


async def test_entity_publishes_designer_capabilities(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """The image entity's attributes carry the designer's HostCapabilities shape.

    This is what the panel wrapper's buildTargets() reads to build a
    designer `HostTarget` -- pixel_width is the gate it uses to decide the
    capability attrs have actually been published (see the panel JS's own
    comment on that check).
    """
    await setup_entry()

    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.attributes["pixel_width"] > 0
    assert state.attributes["pixel_height"] > 0
    assert state.attributes["render_width"] > 0
    assert state.attributes["render_height"] > 0
    assert isinstance(state.attributes["color_map"], dict)
    assert state.attributes["color_map"]
    assert isinstance(state.attributes["available_colors"], list)
    assert state.attributes["available_colors"]


async def test_uploaded_image_is_served(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """The bytes from an upload are served over the image endpoint."""
    await setup_entry()

    _send_image(hass)
    await hass.async_block_till_done()

    client = await hass_client()
    resp = await client.get(hass.states.get(ENTITY).attributes["entity_picture"])

    assert resp.status == 200
    assert resp.headers["Content-Type"] == "image/jpeg"
    assert await resp.read() == JPEG


async def test_image_last_updated_advances_on_a_new_frame(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """A second upload is not served from the first one's cached URL."""
    await setup_entry()

    _send_image(hass)
    await hass.async_block_till_done()
    first = hass.states.get(ENTITY).state

    _send_image(hass, b"\xff\xd8 second frame")
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY).state != first


async def test_queued_frame_is_marked_pending(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """A frame queued for a sleeping tag shows as not yet on the panel.

    The entity deliberately shows the intended frame straight away, so the
    pending flag is what distinguishes it from what the panel is displaying.
    """
    await setup_entry()

    mock_config_entry.runtime_data.delivery.submit_upload(
        prepared=(b"frame", None, MagicMock()),
        refresh_mode=MagicMock(),
        partial_state=MagicMock(),
        use_measured_palettes=False,
        preview_jpeg=JPEG,
        device_id=None,
    )
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY)
    assert state.attributes["pending"] is True
    assert state.attributes["queued_at"] is not None


async def test_delivery_failure_is_surfaced(
    hass: HomeAssistant,
    setup_entry: Callable[[], Awaitable[None]],
) -> None:
    """The last delivery error reaches the entity attributes."""
    await setup_entry()

    async_dispatcher_send(
        hass,
        f"{SIGNAL_PENDING_STATE}_{TEST_ADDRESS}",
        DeliverySnapshot(
            pending=True,
            queued_at=1_700_000_000.0,
            expires_at=None,
            attempts=2,
            last_error="BLE connection failed",
            auth_paused=False,
        ),
    )
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY).attributes["last_error"] == "BLE connection failed"
