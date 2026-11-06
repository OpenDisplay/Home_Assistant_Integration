"""Tests for how source images are normalized when they are loaded."""

import io
import struct

from PIL import Image as PILImage, ImageCms
import pytest

from custom_components.opendisplay.services import (
    _load_image,
    _load_image_from_bytes,
    _pil_to_jpeg,
)

WHITE = (255, 255, 255)
# A mid green: well inside both gamuts, so it maps rather than clips.
SOURCE_GREEN = (120, 160, 100)


def _encode(image: PILImage.Image, fmt: str = "PNG", **params) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format=fmt, **params)
    return buf.getvalue()


def _adobe_rgb_profile() -> bytes:
    """Build a minimal Adobe RGB (1998) matrix/TRC ICC profile, D50-adapted."""

    def s15(value: float) -> bytes:
        return struct.pack(">i", round(value * 65536))

    def xyz(x: float, y: float, z: float) -> bytes:
        return b"XYZ \0\0\0\0" + s15(x) + s15(y) + s15(z)

    name = b"Adobe RGB (1998)\0"
    tags = {
        b"desc": b"desc\0\0\0\0" + struct.pack(">I", len(name)) + name + bytes(12 + 67),
        b"cprt": b"text\0\0\0\0none\0",
        b"wtpt": xyz(0.9642, 1.0, 0.8249),
        b"rXYZ": xyz(0.60974, 0.31111, 0.01947),
        b"gXYZ": xyz(0.20528, 0.62567, 0.06087),
        b"bXYZ": xyz(0.14919, 0.06322, 0.74457),
        b"rTRC": b"curv\0\0\0\0" + struct.pack(">IH", 1, 0x0233),
    }
    tags[b"gTRC"] = tags[b"bTRC"] = tags[b"rTRC"]

    table = struct.pack(">I", len(tags))
    body = b""
    offset = 128 + 4 + 12 * len(tags)
    for signature, data in tags.items():
        padded = data + bytes(-len(data) % 4)
        table += signature + struct.pack(">II", offset + len(body), len(data))
        body += padded

    size = offset + len(body)
    header = bytearray(128)
    header[0:4] = struct.pack(">I", size)
    header[8:12] = bytes([2, 0x40, 0, 0])
    header[12:16] = b"mntr"
    header[16:20] = b"RGB "
    header[20:24] = b"XYZ "
    header[36:40] = b"acsp"
    header[68:80] = s15(0.9642) + s15(1.0) + s15(0.8249)
    return bytes(header) + table + body


@pytest.mark.parametrize(
    "image",
    [
        PILImage.new("RGBA", (4, 4), (0, 0, 0, 0)),
        PILImage.new("LA", (4, 4), (0, 0)),
    ],
    ids=["rgba", "grayscale_alpha"],
)
def test_transparency_loads_as_white(image: PILImage.Image) -> None:
    """A transparent area is white, whatever colour the encoder left beneath it."""
    loaded = _load_image_from_bytes(_encode(image))

    assert loaded.convert("RGB").getpixel((0, 0)) == WHITE


def test_indexed_transparency_loads_as_white() -> None:
    """A transparent palette entry is white too, the GIF and 8-bit PNG case."""
    image = PILImage.new("P", (4, 4), 0)
    image.putpalette([0, 0, 0, 255, 0, 0])

    loaded = _load_image_from_bytes(_encode(image, transparency=0))

    assert loaded.convert("RGB").getpixel((0, 0)) == WHITE


def test_partial_transparency_blends_with_white() -> None:
    """Half-covered black is mid gray on the white panel, not solid black."""
    image = PILImage.new("RGBA", (4, 4), (0, 0, 0, 128))

    red, green, blue = _load_image_from_bytes(_encode(image)).getpixel((0, 0))

    assert red == green == blue
    assert 120 <= red <= 135


def test_opaque_images_keep_their_pixels_and_mode() -> None:
    """An image with nothing to resolve is left exactly as it was."""
    loaded = _load_image_from_bytes(_encode(PILImage.new("L", (4, 4), 42)))

    assert loaded.mode == "L"
    assert loaded.getpixel((0, 0)) == 42


def test_load_from_disk_is_normalized_too(tmp_path) -> None:
    """Local files take the same path as downloaded ones."""
    path = tmp_path / "logo.png"
    path.write_bytes(_encode(PILImage.new("RGBA", (4, 4), (0, 0, 0, 0))))

    assert _load_image(str(path)).convert("RGB").getpixel((0, 0)) == WHITE


def test_preview_of_a_transparent_render_is_white() -> None:
    """A drawcustom layout with a transparent background previews on white."""
    jpeg = _pil_to_jpeg(PILImage.new("RGBA", (16, 16), (0, 0, 0, 0)))

    assert min(PILImage.open(io.BytesIO(jpeg)).getpixel((8, 8))) >= 250


def test_wide_gamut_profile_is_converted_to_srgb() -> None:
    """Adobe RGB values are converted, which makes this green more saturated."""
    source = PILImage.new("RGB", (4, 4), SOURCE_GREEN)

    loaded = _load_image_from_bytes(_encode(source, icc_profile=_adobe_rgb_profile()))

    red, green, blue = loaded.getpixel((0, 0))
    assert red < SOURCE_GREEN[0]
    assert green >= SOURCE_GREEN[1]
    assert blue <= SOURCE_GREEN[2]
    assert "icc_profile" not in loaded.info


def test_transparent_wide_gamut_image_stays_white() -> None:
    """Transparency resolves to white in sRGB as well, not to a tinted white."""
    source = PILImage.new("RGBA", (4, 4), (0, 0, 0, 0))

    loaded = _load_image_from_bytes(_encode(source, icc_profile=_adobe_rgb_profile()))

    assert min(loaded.getpixel((0, 0))) >= 254


def test_srgb_profile_leaves_the_pixels_untouched() -> None:
    """An sRGB source, the common case, skips the conversion entirely."""
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    source = PILImage.new("RGB", (4, 4), SOURCE_GREEN)

    loaded = _load_image_from_bytes(_encode(source, icc_profile=srgb))

    assert loaded.getpixel((0, 0)) == SOURCE_GREEN


def test_unreadable_profile_is_ignored_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A corrupt profile must not stop an otherwise good image being sent."""
    source = PILImage.new("RGB", (4, 4), SOURCE_GREEN)
    data = _encode(source, "JPEG", icc_profile=b"not a real profile", quality=100)

    loaded = _load_image_from_bytes(data)

    assert loaded.size == (4, 4)
    assert "colour profile" in caplog.text
