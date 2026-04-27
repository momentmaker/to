"""Compress an incoming photo into a small JPEG asset.

The output is meant to live in the captures repo as proof-of-artifact, not
for re-reading detail. The LLM-extracted body remains the canonical text
record; this asset just anchors "this is the thing I saw."
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_LONG_EDGE = 1000
JPEG_QUALITY = 75


def compress_for_asset(data: bytes) -> bytes:
    """Resize so the long edge is at most MAX_LONG_EDGE, save as JPEG q=75.

    Honors EXIF orientation so the saved pixels match what a viewer sees.
    Never upscales; small images pass through at original dimensions.
    Raises ValueError if the input isn't a recognizable image.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError(f"not a valid image: {e}") from e

    img = ImageOps.exif_transpose(img)

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    img.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()
