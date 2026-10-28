"""Asset addresses carry the digest of the bytes behind them.

That is what makes a long cache lifetime safe: a deploy changes the address, so a returning
browser keeps what it has and still sees the new file. A request that does not name the current
version has to revalidate, or a stale address could pin yesterday's CSS into a reader.
"""

from app.core.assets import digest, static_url

STYLESHEET = "css/styles.css"


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
