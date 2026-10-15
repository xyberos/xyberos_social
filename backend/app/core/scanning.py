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
"""

import logging
import socket
import struct
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from app.core.config import get_settings
from app.core.media import detect_mime, image_type, looks_like_markup, looks_like_text

logger = logging.getLogger(__name__)

INSTREAM_CHUNK = 64 * 1024
SCANNER_TIMEOUT_SECONDS = 15.0


class ScanUnavailable(RuntimeError):
    """The scanner could not be consulted, so no verdict exists for these bytes."""


@dataclass(frozen=True)
class ScanVerdict:
    clean: bool
    detail: str = ""


class Scanner(Protocol):
    def scan(self, data: bytes, *, declared_mime: str) -> ScanVerdict: ...


class StructuralScanner:
    """Validates structure and type agreement without inspecting for malware."""

    def scan(self, data: bytes, *, declared_mime: str) -> ScanVerdict:
        if not data:
            return ScanVerdict(False, "the upload is empty")
        if looks_like_markup(data):
            return ScanVerdict(False, "markup and SVG are not accepted")
        declared = declared_mime.lower().strip()
        if declared.startswith("image/"):
            detected = image_type(data)
            if detected is None:
                return ScanVerdict(False, "the file is not a readable image")
            if detected != declared:
                return ScanVerdict(False, f"declared {declared} but the bytes are {detected}")
            return ScanVerdict(True)
        detected = detect_mime(data)
        if declared == "application/pdf":
            if detected != "application/pdf":
                return ScanVerdict(False, "the file does not start with a PDF header")
            return ScanVerdict(True)
        if declared == "text/plain":
            if detected is not None:
                return ScanVerdict(False, f"declared {declared} but the bytes are {detected}")
            if not looks_like_text(data):
                return ScanVerdict(False, "the file is not valid UTF-8 text")
            return ScanVerdict(True)
        return ScanVerdict(False, f"{declared} is not an accepted type")


class ClamAvScanner:
    """Streams bytes to clamd. Nothing is executed and no file is written on the scanner."""

    def __init__(self, host: str, port: int, timeout: float = SCANNER_TIMEOUT_SECONDS) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def scan(self, data: bytes, *, declared_mime: str) -> ScanVerdict:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as conn:
                conn.sendall(b"zINSTREAM\0")
                for offset in range(0, len(data), INSTREAM_CHUNK):
                    chunk = data[offset : offset + INSTREAM_CHUNK]
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
