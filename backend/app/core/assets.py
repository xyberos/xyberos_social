"""Asset addresses that change when the file changes.

A stylesheet or script is referenced as `/static/css/styles.css?v=<digest of its bytes>`, so a
deploy that edits the file produces a new address and a browser that already holds the old one
fetches the new one. That is what makes the long, immutable cache lifetime safe: the URL *is*
the version.

The digest is computed from the file on disk, once per process. An asset edited in place while
the process runs keeps its old digest until a restart — which is the same moment the new bytes
would have been read anyway under `--reload`.
"""

from functools import lru_cache
from hashlib import sha256
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = BASE_DIR / "static"

# Short is enough: this names a version, it does not defend against collisions.
DIGEST_LENGTH = 10


@lru_cache(maxsize=None)
def digest(relative_path: str) -> str:
    """The content digest of one file under `static/`, or a marker when it is missing."""
    path = STATIC_DIR / relative_path
    try:
        return sha256(path.read_bytes()).hexdigest()[:DIGEST_LENGTH]
    except OSError:
        # A missing asset is a broken reference; the address still has to be renderable so the
        # page can report it, and `?v=missing` makes the mistake visible in a response.
        return "missing"


def static_url(relative_path: str) -> str:
    """`/static/<path>?v=<digest>`, the address a template should use for an asset."""
    return f"/static/{relative_path}?v={digest(relative_path)}"


def is_current_version(relative_path: str, version: str | None) -> bool:
    """Whether a request names the version this process would serve."""
    return version is not None and version == digest(relative_path)
