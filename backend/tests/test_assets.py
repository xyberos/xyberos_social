"""Asset addresses carry the digest of the bytes behind them.

That is what makes a long cache lifetime safe: a deploy changes the address, so a returning
browser keeps what it has and still sees the new file. A request that does not name the current
version has to revalidate, or a stale address could pin yesterday's CSS into a reader.
"""

from app.core.assets import STATIC_DIR, STYLES_MINIFIED, digest, minified_styles, static_url

STYLESHEET = STYLES_MINIFIED


def test_a_page_names_the_current_version_of_each_asset(client):
    page = client.get("/").text
    version = digest(STYLESHEET)

    assert f'href="/static/{STYLESHEET}?v={version}"' in page
    # Every script the shell loads is versioned too, not just the stylesheet.
    assert page.count("/static/js/") == page.count("?v=") - 2  # stylesheet + apple-touch-icon


def test_the_current_version_is_cached_forever(client):
    response = client.get(f"/static/{STYLESHEET}?v={digest(STYLESHEET)}")

    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert "max-age=31536000" in response.headers["cache-control"]


def test_any_other_version_revalidates(client):
    plain = client.get(f"/static/{STYLESHEET}")
    stale = client.get(f"/static/{STYLESHEET}?v=0000000000")

    for response in (plain, stale):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"


def test_a_missing_asset_still_renders_an_address():
    """A broken reference should be visible in the response, not raise while rendering."""
    assert static_url("nope/absent.css").endswith("?v=missing")


def test_the_digest_is_short_and_content_derived():
    assert len(digest(STYLESHEET)) == 10
    # Two different files do not share a digest, so the address really does name the content.
    assert digest(STYLESHEET) != digest("js/theme.js")


def test_the_minified_sheet_is_current():
    """`python -m app.core.assets --write` regenerates it; a stale copy would ship old rules."""
    on_disk = (STATIC_DIR / STYLES_MINIFIED).read_text(encoding="utf-8")

    assert on_disk == minified_styles(), (
        "css/styles.min.css is out of date; run: python -m app.core.assets --write"
    )


def test_minification_keeps_what_is_content():
    built = minified_styles()
    source = (STATIC_DIR / "css/styles.css").read_text(encoding="utf-8")

    assert "/*" not in built, "a comment survived"
    # Spaces inside calc() are part of the expression, and a quoted string is content.
    assert "calc(100dvh - 200px)" in built
    assert '"Camera off"' in built
    assert 'url("/static/fonts/inter-latin.woff2")' in built
    assert len(built) < len(source)


def test_minification_is_deterministic():
    """A build that produced different bytes each run would churn the asset digest."""
    assert minified_styles() == minified_styles()
