"""What a search engine is told: crawl files, page metadata, and the walls it must respect."""

from conftest import create_account, csrf_from

from app.core.config import get_settings

BROWSER = {"accept": "text/html,application/xhtml+xml"}


def create_community(client, slug: str, visibility: str = "public", description: str = "") -> None:
    page = client.get("/communities/new")
    response = client.post(
        "/communities",
        data={
            "csrf_token": csrf_from(page.text),
            "name": slug.replace("-", " ").title(),
            "slug": slug,
            "description": description or "A community for thoughtful conversation.",
            "visibility": visibility,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def create_community_post(client, slug: str, title: str = "A thoughtful topic") -> str:
    page = client.get(f"/communities/{slug}")
    response = client.post(
        f"/communities/{slug}/posts",
        data={"csrf_token": csrf_from(page.text), "title": title, "body": "Some useful context."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def test_robots_states_the_walls_and_names_the_sitemap(client):
    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "User-agent: *" in body
    for prefix in ("/dashboard", "/conversations", "/people", "/account", "/notifications"):
        assert f"Disallow: {prefix}" in body
    assert "Disallow: /communities/*/reports" in body
    assert body.strip().endswith("/sitemap.xml")


def test_sitemap_lists_only_what_a_signed_out_visitor_can_read(client_factory):
    owner = client_factory()
    assert create_account(owner, "owner@example.com", "owner").status_code == 303
    create_community(owner, "open-garden")
    create_community(owner, "quiet-room", visibility="private")
    post_id = create_community_post(owner, "open-garden")
    create_community_post(owner, "quiet-room", title="Inside only")

    anonymous = client_factory()
    response = anonymous.get("/sitemap.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    body = response.text
    assert "/communities/open-garden<" in body
    assert f"/communities/open-garden/posts/{post_id}<" in body
    # A private community answers 404 to a crawler, so a sitemap entry would be a broken promise.
    assert "quiet-room" not in body


def publish_personal(client, audience: str) -> str:
    """Publish a personal update, returning the post id."""
    page = client.get("/posts/new")
    response = client.post(
        "/posts",
        data={
            "csrf_token": csrf_from(page.text),
            "title": "",
            "body": f"An update for {audience}.",
            "audience": audience,
            "return_to": "/posts/new",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


def test_sitemap_lists_an_open_update_and_no_other_audience(client_factory):
    author = client_factory()
    assert create_account(author, "author@example.com", "author").status_code == 303

    open_update = publish_personal(author, "public")
    closed_updates = {
        publish_personal(author, "everyone"),
        publish_personal(author, "followers"),
        publish_personal(author, "only_me"),
    }

    body = client_factory().get("/sitemap.xml").text

    assert f"/posts/{open_update}<" in body
    for closed in closed_updates:
        assert f"/posts/{closed}<" not in body


def test_public_pages_carry_a_description_canonical_and_share_card(client):
    base_url = get_settings().app_base_url.rstrip("/")
    home = client.get("/", headers=BROWSER)

    assert home.status_code == 200
    assert '<meta name="description" content="Social+' in home.text
    assert f'<link rel="canonical" href="{base_url}/">' in home.text
    assert '<meta name="robots" content="index, follow">' in home.text
    assert '<meta property="og:title"' in home.text
    assert '<meta name="twitter:card"' in home.text
    assert '<script type="application/ld+json">' in home.text


def test_community_page_describes_itself_and_hides_a_private_space(client_factory):
    owner = client_factory()
    assert create_account(owner, "owner@example.com", "owner").status_code == 303
    create_community(owner, "open-garden", description="Gardeners trading seeds and cuttings.")
    create_community(owner, "quiet-room", visibility="private")

    public = owner.get("/communities/open-garden", headers=BROWSER)
    assert 'content="Gardeners trading seeds and cuttings."' in public.text
    assert '<meta name="robots" content="index, follow">' in public.text

    private = owner.get("/communities/quiet-room", headers=BROWSER)
    assert '<meta name="robots" content="noindex, nofollow">' in private.text


def test_a_walled_page_tells_a_crawler_to_stay_out(client):
    response = client.get("/dashboard", headers=BROWSER, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_a_missing_page_answers_in_the_shell_while_an_api_client_keeps_json(client):
    page = client.get("/no-such-page", headers=BROWSER)

    assert page.status_code == 404
    assert "text/html" in page.headers["content-type"]
    assert '<meta name="robots" content="noindex, follow">' in page.text
    assert "We could not find that page" in page.text

    api = client.get("/no-such-page", headers={"accept": "application/json"})
    assert api.status_code == 404
    assert api.json()["detail"] == "Not Found"


def test_sign_in_page_carries_the_product_name(client):
    page = client.get("/login", headers=BROWSER)

    assert "<title>Sign in | Social+</title>" in page.text
    assert "X-Social" not in page.text
