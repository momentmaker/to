from __future__ import annotations

import io

import pytest
from PIL import Image

from bot.image_resize import MAX_LONG_EDGE, compress_for_asset


def _make_image(w: int, h: int, fmt: str = "PNG", color=(80, 120, 200)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_tall_portrait_downscaled_to_max_long_edge():
    src = _make_image(2000, 4000, fmt="PNG")
    out = compress_for_asset(src)
    img = _open(out)
    assert img.format == "JPEG"
    assert max(img.size) == MAX_LONG_EDGE
    # aspect preserved (within 1px tolerance for rounding)
    assert abs(img.size[1] / img.size[0] - 2.0) < 0.01


def test_wide_landscape_downscaled_to_max_long_edge():
    src = _make_image(4000, 2000, fmt="PNG")
    out = compress_for_asset(src)
    img = _open(out)
    assert img.format == "JPEG"
    assert max(img.size) == MAX_LONG_EDGE
    assert abs(img.size[0] / img.size[1] - 2.0) < 0.01


def test_already_small_image_not_upscaled():
    src = _make_image(500, 300, fmt="PNG")
    out = compress_for_asset(src)
    img = _open(out)
    assert img.format == "JPEG"
    assert img.size == (500, 300)


def test_png_input_returns_jpeg_bytes():
    src = _make_image(800, 600, fmt="PNG")
    out = compress_for_asset(src)
    # JPEG magic bytes
    assert out[:3] == b"\xff\xd8\xff"


def test_size_under_100kb_for_typical_phone_photo():
    src = _make_image(3024, 4032, fmt="JPEG", color=(120, 160, 220))
    out = compress_for_asset(src)
    # solid-color 1000px image at q=75 is well under 100KB; this guards
    # against regressions where someone bumps the long edge or quality.
    assert len(out) < 100 * 1024


def test_exif_orientation_applied():
    """Pillow's ImageOps.exif_transpose should rotate the saved pixels so
    consumers don't need EXIF awareness. We simulate this by saving a tall
    image with an orientation tag claiming it should be rotated 90°."""
    img = Image.new("RGB", (400, 200), (255, 0, 0))
    # paint a stripe so we can detect rotation
    for x in range(50):
        for y in range(200):
            img.putpixel((x, y), (0, 255, 0))

    exif = img.getexif()
    exif[0x0112] = 6  # Orientation = 90° CW
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())

    out = compress_for_asset(buf.getvalue())
    rotated = _open(out)
    # after applying orientation, the image should be portrait, not landscape
    assert rotated.size[1] > rotated.size[0]


def test_returns_bytes_unchanged_type():
    src = _make_image(400, 400, fmt="PNG")
    out = compress_for_asset(src)
    assert isinstance(out, bytes)


def test_rejects_non_image_input():
    with pytest.raises(ValueError):
        compress_for_asset(b"not an image at all")


def test_rejects_decompression_bomb(monkeypatch):
    """A pathological image larger than Pillow's MAX_IMAGE_PIXELS is treated
    as 'not a recognizable image' rather than crashing the photo handler."""
    src = _make_image(2000, 2000, fmt="PNG")
    # Force any non-trivial image to count as a bomb so we don't have to
    # generate gigabytes of test bytes.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ValueError):
        compress_for_asset(src)
