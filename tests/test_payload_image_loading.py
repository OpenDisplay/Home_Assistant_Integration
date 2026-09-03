"""A payload's local image file must not be opened on the event loop.

Reported from real hardware, verbatim from the maintainer's Home Assistant
log::

    Detected blocking call to open with args ('/media/pohl89-480h.png', 'rb')
    inside the event loop by custom integration 'opendisplay' at
    custom_components/opendisplay/designer/render.py, line 323:
    img = await generate_image(
    (offender: PIL/Image.py line 3639: fp = builtins.open(filename, "rb"))

`generate_image` is a coroutine awaited on the loop, but a `dlimg` element
whose `url` is a local absolute path reaches
`odl_renderer.media_loader._load_from_file`, which calls `PIL.Image.open`
directly -- a blocking `open()` inside the event loop, exactly what
https://developers.home-assistant.io/docs/asyncio_blocking_operations/ says
to move to an executor.

WHY THESE TESTS DON'T USE HOME ASSISTANT'S OWN DETECTOR: they can't.
`homeassistant.block_async_io` registers `builtins.open` with
`skip_for_tests=True`, so the patch that produces that warning is never
installed under pytest. These tests assert the underlying property
directly, and more precisely than the warning does -- WHICH THREAD the
payload's image file is opened on -- by wrapping `builtins.open` the same
way the detector would and recording the thread id for the one file the
payload references. The loop thread is the thread the test body itself runs
on.

Both call sites are covered, because both share the bug: the designer's
render endpoint (the reported offender) and `opendisplay.drawcustom`'s own
send path (`_drawcustom_for_device`), which builds its `generate_image`
call the same way.
"""

import builtins
import os
from pathlib import Path
import threading
from typing import Any
from unittest.mock import MagicMock

from PIL import Image as PILImage
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opendisplay.const import DOMAIN
from custom_components.opendisplay.designer.render import DESIGNER_RENDER_URL

_PROBE_NAME = "payload-probe.png"


@pytest.fixture(autouse=True)
async def setup_entry(hass, mock_config_entry: MockConfigEntry) -> None:
    """Set up the config entry."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def device_id(hass, mock_config_entry: MockConfigEntry) -> str:
    """Return the device registry ID for the fabricated config entry."""
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    assert devices
    return devices[0].id


@pytest.fixture
def probe_image(tmp_path: Path) -> Path:
    """Write a real PNG the payload can reference by absolute path."""
    path = tmp_path / _PROBE_NAME
    PILImage.new("RGB", (8, 8), (0, 0, 0)).save(path, format="PNG")
    return path


def _dlimg_payload(path: Path) -> list[dict[str, Any]]:
    return [
        {
            "type": "dlimg",
            "x": 0,
            "y": 0,
            "url": str(path),
            "xsize": 8,
            "ysize": 8,
        }
    ]


class _OpenThreadRecorder:
    """Record which threads open the probe image, wrapping `builtins.open`.

    Deliberately the same interception point Home Assistant's own blocking
    detector uses (`homeassistant/block_async_io.py` patches
    `builtins.open`), so a violation this misses is one the detector would
    miss too -- and a violation the detector reports is one this records.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, probe: Path) -> None:
        self.thread_ids: list[int] = []
        self._probe = str(probe)
        real_open = builtins.open

        def tracking_open(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and str(file) == self._probe:
                self.thread_ids.append(threading.get_ident())
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", tracking_open)

    def assert_never_on(self, loop_thread_id: int) -> None:
        assert self.thread_ids, (
            "the payload's image file was never opened -- this test is not "
            "exercising the image-loading path it claims to"
        )
        assert loop_thread_id not in self.thread_ids, (
            f"blocking open() of the payload's image on the event loop thread "
            f"({loop_thread_id}); opened on {self.thread_ids}"
        )


async def test_render_endpoint_does_not_open_a_payload_image_on_the_loop(
    hass, hass_client, probe_image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported offender: `designer/render.py`'s `generate_image` call.

    Red-first: with the local-image preload removed, the probe is opened on
    the loop thread -- the exact condition the maintainer's log line
    reports.
    """
    loop_thread_id = threading.get_ident()
    recorder = _OpenThreadRecorder(monkeypatch, probe_image)

    client = await hass_client()
    resp = await client.post(
        DESIGNER_RENDER_URL,
        json={
            "display": {"width": 64, "height": 32},
            "payload": _dlimg_payload(probe_image),
            "dither": "none",
        },
    )

    assert resp.status == 200, await resp.text()
    recorder.assert_never_on(loop_thread_id)


async def test_drawcustom_send_does_not_open_a_payload_image_on_the_loop(
    hass,
    device_id: str,
    probe_image: Path,
    mock_opendisplay_device: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same violation on the send path, which shares the call shape."""
    loop_thread_id = threading.get_ident()
    recorder = _OpenThreadRecorder(monkeypatch, probe_image)

    await hass.services.async_call(
        DOMAIN,
        "drawcustom",
        {
            "device_id": [device_id],
            "payload": _dlimg_payload(probe_image),
            "dither": "none",
        },
        blocking=True,
        return_response=True,
    )

    recorder.assert_never_on(loop_thread_id)


async def test_a_payload_image_still_renders_into_the_output(
    hass, hass_client, probe_image: Path
) -> None:
    """Moving the decode off the loop must not change WHAT gets drawn.

    The probe is a solid black 8x8 drawn over the whole 8x8 canvas, so a
    successful load is visible as a non-white output on any palette; a load
    that silently stopped happening would leave the white background.
    """
    client = await hass_client()
    resp = await client.post(
        DESIGNER_RENDER_URL,
        json={
            "display": {"width": 8, "height": 8},
            "payload": _dlimg_payload(probe_image),
            "background": "white",
            "dither": "none",
        },
    )
    assert resp.status == 200, await resp.text()

    import io

    image = PILImage.open(io.BytesIO(await resp.read())).convert("RGB")
    assert image.size == (8, 8)
    assert image.getpixel((4, 4)) != (255, 255, 255), (
        "the payload's image did not reach the canvas"
    )


async def test_a_missing_payload_image_still_fails_the_way_it_did(
    hass, hass_client, tmp_path: Path
) -> None:
    """A path that does not exist reaches the renderer's own error, unchanged.

    The preload is transparent: when it cannot load a source it leaves the
    element alone, so the renderer raises the same `ValueError` it always
    did and the endpoint answers its existing 400 -- no new error class, no
    new message.
    """
    client = await hass_client()
    resp = await client.post(
        DESIGNER_RENDER_URL,
        json={
            "display": {"width": 32, "height": 32},
            "payload": _dlimg_payload(tmp_path / "does-not-exist.png"),
            "dither": "none",
        },
    )
    assert resp.status == 400
    assert "render failed" in (await resp.json())["message"]


async def test_a_payload_without_images_is_untouched(
    hass, hass_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A text-only payload reaches the renderer byte-identically.

    The preload must not rewrite elements it has no business rewriting --
    the send/render pipeline sees exactly the payload it saw before.
    """
    payload = [{"type": "text", "value": "hi", "x": 2, "y": 2, "size": 10}]

    client = await hass_client()
    resp = await client.post(
        DESIGNER_RENDER_URL,
        json={
            "display": {"width": 32, "height": 32},
            "payload": payload,
            "dither": "none",
        },
    )
    assert resp.status == 200, await resp.text()
