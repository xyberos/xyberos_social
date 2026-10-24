"""Attachment type detection, the allowlist, and image metadata stripping.

The type of an upload is decided from its bytes. A declared content type and a filename are
claims, not evidence, so both are compared against what the bytes actually are and a
mismatch is a rejection rather than a coercion.

The comparison is made against a *family* rather than a single mime type, because one
container names more than one of the formats people upload: an `ftyp` box is the honest
reading of `video/mp4` and `audio/mp4` alike, and telling them apart from a header would be a
guess dressed as a check. Families are what the bytes prove; the declared type stays the
client's choice within a family, and it is what the response header and the rendering hint
come from.

SVG and HTML are not merely scanned, they are refused: both execute in a browser, and no
scanner verdict makes them safe to serve from the application's origin.
"""

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

# A 10 MB image can still decode to something that exhausts memory, so the decoded area is
# bounded before any pixel data is touched.
MAX_IMAGE_PIXELS = 40_000_000
READ_CHUNK = 64 * 1024

# A preview is shown at bubble or tile size, so 640 px on the long edge covers a retina tile
# while costing a fraction of the original. The derivative is only kept when it saves enough
# bytes to justify a second object.
THUMBNAIL_MAX_EDGE = 640
THUMBNAIL_QUALITY = 80
THUMBNAIL_MIN_SAVING_BYTES = 16 * 1024
THUMBNAIL_MIME = "image/webp"

# Enough of a container header to name it: sniffing reads the first box, and an EBML DocType
# sits inside the first few hundred bytes.
SAMPLE_BYTES = 512 * 1024

IMAGE = "image"
VIDEO = "video"
AUDIO = "audio"
DOCUMENT = "document"

KINDS = (IMAGE, VIDEO, AUDIO, DOCUMENT)


class UnsupportedMedia(Exception):
    """The bytes are not one of the accepted attachment types."""


@dataclass(frozen=True)
class MediaType:
    mime: str
    extension: str
    # Inline types are the ones a page renders; the rest are offered as a download.
    inline: bool
    kind: str
    # What `detect_family` must answer for these bytes to be this type.
    family: str


ACCEPTED_TYPES: dict[str, MediaType] = {
    "image/jpeg": MediaType("image/jpeg", "jpg", True, IMAGE, "jpeg"),
    "image/png": MediaType("image/png", "png", True, IMAGE, "png"),
    "image/gif": MediaType("image/gif", "gif", True, IMAGE, "gif"),
    "image/webp": MediaType("image/webp", "webp", True, IMAGE, "webp"),
    # MP4 and M4A are one container: the brand box states what a file claims, and a player
    # decides from the streams inside. Both spellings live in the same family.
    "video/mp4": MediaType("video/mp4", "mp4", True, VIDEO, "isobmff"),
    "audio/mp4": MediaType("audio/mp4", "m4a", True, AUDIO, "isobmff"),
    "video/webm": MediaType("video/webm", "webm", True, VIDEO, "webm"),
    "audio/webm": MediaType("audio/webm", "weba", True, AUDIO, "webm"),
    "audio/mpeg": MediaType("audio/mpeg", "mp3", True, AUDIO, "mpeg-audio"),
    "audio/ogg": MediaType("audio/ogg", "ogg", True, AUDIO, "ogg"),
    "audio/wav": MediaType("audio/wav", "wav", True, AUDIO, "wav"),
    "application/pdf": MediaType("application/pdf", "pdf", False, DOCUMENT, "pdf"),
    "text/plain": MediaType("text/plain", "txt", False, DOCUMENT, "text"),
}

_IMAGE_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}

DECLARED_CONTENT_TYPES = tuple(ACCEPTED_TYPES)

# What the composer's picker offers. Audio and video are the kinds this phase adds.
MEDIA_CONTENT_TYPES = tuple(
    mime for mime, media in ACCEPTED_TYPES.items() if media.kind in {IMAGE, VIDEO, AUDIO}
)


def is_accepted(mime: str) -> bool:
    return mime in ACCEPTED_TYPES


def kind_for(mime: str) -> str | None:
    media = ACCEPTED_TYPES.get(mime)
    return media.kind if media else None


def family_for(mime: str) -> str | None:
    media = ACCEPTED_TYPES.get(mime)
    return media.family if media else None


def detect_family(data: bytes) -> str | None:
    """Name the container from its leading bytes; the extension and declaration are ignored."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "gif"
    if data[:4] == b"RIFF":
        # One RIFF header covers both of the containers this application accepts.
        if data[8:12] == b"WEBP":
            return "webp"
        if data[8:12] == b"WAVE":
            return "wav"
        return None
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data[4:8] == b"ftyp":
        return "isobmff"
    if data.startswith(b"\x1aE\xdf\xa3"):
        # EBML is Matroska's shell. Only the WebM profile is accepted: a Matroska file is
        # refused rather than relabelled, because a browser will not play it.
        return "webm" if b"webm" in data[:512] else None
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "mpeg-audio"
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
            # The re-encode drops the EXIF block, so the orientation it carried has to be
            # applied to the pixels first or a phone photo would arrive sideways.
            upright = ImageOps.exif_transpose(image)
            buffer = io.BytesIO()
            save_options: dict[str, object] = {"format": format_name}
            if format_name in {"JPEG", "WEBP"}:
                # These plugins copy `info` metadata unless it is explicitly cleared.
                save_options.update(exif=b"", icc_profile=None)
            upright.save(buffer, **save_options)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        raise UnsupportedMedia("the image could not be normalized") from error


def render_thumbnail(data: bytes, mime: str) -> bytes | None:
    """A small WebP preview of already-verified bytes, or `None` when one would not help.

    This runs on the normalized payload, never on the raw upload: the pixels have already been
    re-encoded once, so the derivative inherits the stripped metadata and the same verdict.
    A preview that is not meaningfully smaller than the original is not stored at all — the
    caller falls back to the original image.
    """
    if kind_for(mime) != IMAGE:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.mode not in {"RGB", "RGBA"}:
                has_alpha = image.mode in {"LA", "PA"} or "transparency" in image.info
                image = image.convert("RGBA" if has_alpha else "RGB")
            longest = max(image.size)
            scale = min(1.0, THUMBNAIL_MAX_EDGE / longest)
            if scale < 1.0:
                image = image.resize(
                    (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            buffer = io.BytesIO()
            image.save(buffer, format="WEBP", quality=THUMBNAIL_QUALITY, method=4)
            thumbnail = buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return None
    if len(data) - len(thumbnail) < THUMBNAIL_MIN_SAVING_BYTES:
        return None
    return thumbnail
