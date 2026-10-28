"""The icon font is a subset, so an unlisted icon silently renders as its own name.

`static/fonts/icons.txt` is the list the subset was built from, and
`static/fonts/material-symbols-rounded.woff2` carries only those glyphs. Adding an icon to a page
without rebuilding does not fail a browser request — it prints "expand_more" where an arrow
belongs, which is exactly how `check` and `expand_more` were missed once. These tests make that
mismatch fail here instead.
"""

import pathlib
import re

FONT = pathlib.Path("backend/app/static/fonts/material-symbols-rounded.woff2")
MANIFEST = pathlib.Path("backend/app/static/fonts/icons.txt")
APP_ROOT = pathlib.Path("backend/app")

# The class is not always the only one on the element: `material-symbols-rounded
# theme-option-check` is still a glyph, and a pattern that expects the class to end the attribute
# silently drops it.
ELEMENT = re.compile(
    r'class="[^"]*\bmaterial-symbols-rounded\b[^"]*"[^>]*>\s*([a-z][a-z0-9_]*)\s*<'
)
PYTHON_ICON = re.compile(r'["\']icon["\']\s*:\s*["\']([a-z][a-z0-9_]*)["\']')

# Icons a script assigns at runtime, where no static markup carries the name.
SCRIPT_ICONS = {
    "brightness_auto",
    "chevron_left",
    "chevron_right",
    "close",
    "dark_mode",
    "download",
    "light_mode",
    "mic",
    "mic_off",
    "videocam",
    "videocam_off",
}


def icons_in_pages() -> set[str]:
    names: set[str] = set()
    for path in APP_ROOT.rglob("*"):
        if path.suffix not in {".html", ".js", ".py"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        names.update(ELEMENT.findall(text))
        names.update(PYTHON_ICON.findall(text))
    return names | SCRIPT_ICONS


def manifest_icons() -> set[str]:
    return set(MANIFEST.read_text(encoding="utf-8").split())


def test_the_subset_manifest_matches_the_pages():
    assert FONT.exists(), "the icon font is missing"
    assert MANIFEST.exists(), "the subset manifest is missing"

    named = icons_in_pages()
    built = manifest_icons()

    # Both directions matter: an icon named in a page but not built renders as text, and one
    # built but no longer used is weight nobody needs.
    assert named - built == set(), f"named but not in the subset: {sorted(named - built)}"
    assert built - named == set(), f"in the subset but unused: {sorted(built - named)}"


def test_the_font_is_self_hosted_and_preloaded():
    head = pathlib.Path("backend/app/templates/base.html").read_text(encoding="utf-8")

    # No third-party stylesheet or font origin on the critical path.
    assert "fonts.googleapis.com" not in head
    assert "fonts.gstatic.com" not in head
    assert 'href="/static/fonts/inter-latin.woff2"' in head
    assert 'href="/static/fonts/material-symbols-rounded.woff2"' in head
    assert head.count('as="font"') == 2


def test_the_policy_no_longer_allows_a_font_origin():
    policy = pathlib.Path("backend/app/api/middleware.py").read_text(encoding="utf-8")

    assert "font-src 'self';" in policy
    assert "fonts.gstatic.com" not in policy
    assert "fonts.googleapis.com" not in policy


def test_the_icon_font_stays_small():
    """The published file carries 6,660 glyphs; the subset is what keeps a phone from fetching
    1.4 MB to draw 58 pictures."""
    assert FONT.stat().st_size < 60_000
