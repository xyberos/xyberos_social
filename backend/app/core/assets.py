"""Asset addresses that change when the file changes, and the minified stylesheet.

A stylesheet or script is referenced as `/static/css/styles.css?v=<digest of its bytes>`, so a
deploy that edits the file produces a new address and a browser that already holds the old one
fetches the new one. That is what makes the long, immutable cache lifetime safe: the URL *is*
the version.

The digest is computed from the file on disk, once per process. An asset edited in place while
the process runs keeps its old digest until a restart — which is the same moment the new bytes
would have been read anyway under `--reload`.
"""

import sys
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = BASE_DIR / "static"

# Short is enough: this names a version, it does not defend against collisions.
DIGEST_LENGTH = 10

STYLES_SOURCE = "css/styles.css"
STYLES_MINIFIED = "css/styles.min.css"


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


def minify_css(source: str) -> str:
    """Strip comments and redundant whitespace, without touching anything that is content.

    A blind regex is wrong here: this sheet contains `content: "Camera off"`, font names in
    quotes, and `calc(100dvh - 200px)`, where the spaces are part of the expression. The scanner
    below only removes whitespace outside strings, only around `{`, `}`, `;` and `,` — never
    around `:` (a descendant selector may use it) or inside `calc()` — and drops the last `;` of
    a block.
    """
    out: list[str] = []
    index = 0
    length = len(source)
    quote: str | None = None
    pending_space = False

    def flush_space() -> None:
        nonlocal pending_space
        if pending_space:
            # One space stands in for a run; it is dropped below when punctuation makes it
            # redundant.
            if out and out[-1] not in "{;," and index < length and source[index] not in "};,":
                out.append(" ")
            pending_space = False

    while index < length:
        char = source[index]
        if quote is not None:
            out.append(char)
            if char == quote and source[index - 1] != "\\":
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            flush_space()
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "/" and source.startswith("/*", index):
            end = source.find("*/", index + 2)
            index = length if end == -1 else end + 2
            pending_space = True
            continue
        if char.isspace():
            pending_space = True
            index += 1
            continue
        flush_space()
        out.append(char)
        index += 1

    text = "".join(out)
    # A `;` immediately before `}` closes nothing.
    return text.replace(";}", "}")


def minified_styles() -> str:
    return minify_css((STATIC_DIR / STYLES_SOURCE).read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    """`python -m app.core.assets` rewrites the minified sheet from its source."""
    if argv[1:] not in ([], ["--write"]):
        print("usage: python -m app.core.assets [--write]", file=sys.stderr)
        return 2
    text = minified_styles()
    if argv[1:] == ["--write"]:
        (STATIC_DIR / STYLES_MINIFIED).write_text(text, encoding="utf-8")
        print(f"wrote {STYLES_MINIFIED}: {(STATIC_DIR / STYLES_MINIFIED).stat().st_size} bytes")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
