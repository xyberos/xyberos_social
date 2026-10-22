"""The scan gate an upload passes before it can ever be downloaded.

Two scanners implement the gate:

* `StructuralScanner` decides what the bytes are and refuses anything that is not on the
  allowlist, refusing markup and executables outright and rejecting a declared type that
  does not match the detected one. It is the default outside production.
* `ClamAvScanner` streams the bytes to clamd over its INSTREAM protocol and treats any
  signature match as infected.

A scanner that cannot be reached is neither a pass nor a rejection: the caller leaves the
attachment in `pending_scan` and answers the upload with a retryable failure, because
promoting an unscanned object would break the promise that an unvalidated upload is never
downloadable.

Both scanners answer `scan` for bytes already in hand and `scan_stream` for an upload that
is too large to hold: video and audio are read in chunks, hashed as they pass, and streamed
to the scanner instead of being buffered whole.
"""

import logging
import socket
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from app.core.config import get_settings
from app.core.media import (
    ACCEPTED_TYPES,
    AUDIO,
    DOCUMENT,
    IMAGE,
    SAMPLE_BYTES,
    VIDEO,
    detect_family,
    family_for,
    image_type,
    looks_like_markup,
    looks_like_text,
)

logger = logging.getLogger(__name__)

INSTREAM_CHUNK = 64 * 1024
SCANNER_TIMEOUT_SECONDS = 15.0

# Kinds whose structure is proven from the header alone, so they never need buffering. An
# image is decoded and a document is probed in full, so both stay on the in-memory path.
STREAMING_KINDS = frozenset({VIDEO, AUDIO})


class ScanUnavailable(RuntimeError):
    """The scanner could not be consulted, so no verdict exists for these bytes."""


@dataclass(frozen=True)
class ScanVerdict:
    clean: bool
    detail: str = ""


class Scanner(Protocol):
    def scan(self, data: bytes, *, declared_mime: str) -> ScanVerdict: ...

    def scan_stream(
        self, chunks: Iterable[bytes], *, declared_mime: str
    ) -> ScanVerdict: ...


class StructuralScanner:
    """Validates structure and type agreement without inspecting for malware."""

    def scan(self, data: bytes, *, declared_mime: str) -> ScanVerdict:
        if not data:
            return ScanVerdict(False, "the upload is empty")
        if looks_like_markup(data):
            return ScanVerdict(False, "markup and SVG are not accepted")
        declared = declared_mime.lower().strip()
        media = ACCEPTED_TYPES.get(declared)
        if media is None:
            return ScanVerdict(False, f"{declared} is not an accepted type")
        if media.kind == IMAGE:
            detected = image_type(data)
            if detected is None:
                return ScanVerdict(False, "the file is not a readable image")
            if family_for(detected) != media.family:
                return ScanVerdict(False, f"declared {declared} but the bytes are {detected}")
            return ScanVerdict(True)
        if media.kind == DOCUMENT:
            if declared == "text/plain":
                if detect_family(data) is not None:
                    return ScanVerdict(
                        False, f"declared {declared} but the bytes are {detect_family(data)}"
                    )
                if not looks_like_text(data):
                    return ScanVerdict(False, "the file is not valid UTF-8 text")
                return ScanVerdict(True)
            if detect_family(data) != media.family:
                return ScanVerdict(False, f"declared {declared} but the bytes are not")
            return ScanVerdict(True)
        detected = detect_family(data)
        # The whole point of the family is that a container is named, not guessed: an
        # unrecognised header is a rejection even when the client named a plausible type.
        if detected != media.family:
            return ScanVerdict(
                False,
                f"declared {declared} but the bytes are {detected or 'not a recognised container'}",
            )
        return ScanVerdict(True)

    def scan_stream(self, chunks: Iterable[bytes], *, declared_mime: str) -> ScanVerdict:
        """Prove the container from its head, and drain the rest without keeping it."""
        media = ACCEPTED_TYPES.get(declared_mime.lower().strip())
        if media is None or media.kind not in STREAMING_KINDS:
            # Everything else needs all of its bytes to be judged, so it is buffered here.
            return self.scan(b"".join(chunks), declared_mime=declared_mime)
        head = b""
        for chunk in chunks:
            if len(head) < SAMPLE_BYTES:
                head += chunk[: SAMPLE_BYTES - len(head)]
        return self.scan(head, declared_mime=declared_mime)


class ClamAvScanner:
    """Streams bytes to clamd. Nothing is executed and no file is written on the scanner."""

    def __init__(self, host: str, port: int, timeout: float = SCANNER_TIMEOUT_SECONDS) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def scan(self, data: bytes, *, declared_mime: str) -> ScanVerdict:
        return self.scan_stream((data,), declared_mime=declared_mime)

    def scan_stream(self, chunks: Iterable[bytes], *, declared_mime: str) -> ScanVerdict:
        """Stream chunks to clamd as they arrive, so an upload is never held whole in memory."""
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as conn:
                conn.sendall(b"zINSTREAM\0")
                for chunk in chunks:
                    if not chunk:
                        continue
                    conn.sendall(struct.pack("!I", len(chunk)) + chunk)
                conn.sendall(struct.pack("!I", 0))
                # clamd terminates the reply with a NUL byte, which is not whitespace.
                response = conn.recv(4096).decode("utf-8", "replace").replace("\x00", "").strip()
        except OSError as error:
            logger.warning("scanner unreachable", extra={"event": "scan.unavailable"})
            raise ScanUnavailable("the scanner is unreachable") from error

        if response.endswith("OK"):
            return ScanVerdict(True)
        if "FOUND" in response:
            return ScanVerdict(False, f"the scanner rejected the file: {response}")
        raise ScanUnavailable(f"unexpected scanner response: {response!r}")


@lru_cache
def get_scanner() -> Scanner:
    settings = get_settings()
    if settings.clamav_host:
        return ClamAvScanner(settings.clamav_host, settings.clamav_port)
    return StructuralScanner()
