"""One single-frame decoding policy: visible pixels on an opaque white background.

Callers retain the original bytes separately. Verification checks file structure;
loading checks pixel data, including JPEGs whose verify() alone accepts truncation.
"""

from io import BytesIO

from PIL import Image, UnidentifiedImageError

from .errors import ExtractionError


def decode_image(data: bytes) -> tuple[Image.Image, str | None, bool]:
    """Return RGB pixels, original format and whether transparency was composited."""
    try:
        with Image.open(BytesIO(data)) as original:
            if getattr(original, "n_frames", 1) != 1:
                raise ExtractionError("Use a single-frame order image.", code="invalid_image")
            image_format = original.format
            original.verify()
        with Image.open(BytesIO(data)) as original:
            original.load()
            transparent = "A" in original.getbands() or "transparency" in original.info
            if transparent:
                visible = Image.new("RGBA", original.size, "white")
                visible.alpha_composite(original.convert("RGBA"))
                return visible.convert("RGB"), image_format, True
            return original.convert("RGB"), image_format, False
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, IndexError,
            Image.DecompressionBombError) as exc:
        raise ExtractionError(
            f"The file cannot be decoded as an order image ({type(exc).__name__}).",
            code="invalid_image",
        ) from exc
