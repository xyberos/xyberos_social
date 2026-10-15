"""Attachment type detection, the allowlist, and image metadata stripping.

The type of an upload is decided from its bytes. A declared content type and a filename are
claims, not evidence, so both are compared against what the bytes actually are and a
mismatch is a rejection rather than a coercion.

SVG and HTML are not merely scanned, they are refused: both execute in a browser, and no
scanner verdict makes them safe to serve from the application's origin.
"""

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

# A 10 MB image can still decode to something that exhausts memory, so the decoded area is
# bounded before any pixel data is touched.
MAX_IMAGE_PIXELS = 40_000_000
READ_CHUNK = 64 * 1024


class UnsupportedMedia(Exception):
    """The bytes are not one of the accepted attachment types."""


@dataclass(frozen=True)
class MediaType:
    mime: str
    extension: str
    inline: bool
    image: bool


ACCEPTED_TYPES: dict[str, MediaType] = {
    "image/jpeg": MediaType("image/jpeg", "jpg", True, True),
    "image/png": MediaType("image/png", "png", True, True),
    "image/gif": MediaType("image/gif", "gif", True, True),
    "image/webp": MediaType("image/webp", "webp", True, True),
    "application/pdf": MediaType("application/pdf", "pdf", False, False),
    "text/plain": MediaType("text/plain", "txt", False, False),
}

_IMAGE_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}

DECLARED_CONTENT_TYPES = tuple(ACCEPTED_TYPES)


def is_accepted(mime: str) -> bool:
    return mime in ACCEPTED_TYPES


def detect_mime(data: bytes) -> str | None:
    """Sniff the leading bytes; the extension and the declaration are ignored on purpose."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def looks_like_markup(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(
        (b"<svg", b"<?xml", b"<!doctype", b"<html", b"<head", b"<body", b"<script")
    )


def looks_like_text(data: bytes) -> bool:
    """Plain text is the only accepted type without a magic number, so it is probed."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def image_type(data: bytes) -> str | None:
    """Decode the header to learn what the image really is, or `None` if it is not one."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.size[0] * image.size[1] > MAX_IMAGE_PIXELS:
                raise UnsupportedMedia("the image is larger than the accepted pixel budget")
            image.verify()
            return _IMAGE_FORMATS.get(image.format or "")
    except UnsupportedMedia:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return None


def strip_image_metadata(data: bytes, mime: str) -> bytes:
    """Re-encode the pixels so a photo cannot carry location, device, or author metadata.

    Re-encoding also normalizes the file, so a polyglot that decodes as an image loses
    whatever was appended to it. An animated GIF keeps its first frame only: image
    derivatives and animation handling are deferred within this phase.
    """
    format_name = next(
        (name for name, detected in _IMAGE_FORMATS.items() if detected == mime), None
    )
    if format_name is None:
        raise UnsupportedMedia(f"{mime} is not an image")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            buffer = io.BytesIO()
            save_options: dict[str, object] = {"format": format_name}
            if format_name in {"JPEG", "WEBP"}:
                # These plugins copy `info` metadata unless it is explicitly cleared.
                save_options.update(exif=b"", icc_profile=None)
            image.save(buffer, **save_options)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        raise UnsupportedMedia("the image could not be normalized") from error
