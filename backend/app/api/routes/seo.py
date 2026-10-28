"""The crawl files: which URLs may be fetched, and where the public pages are.

Both answer anonymously and both are cheap: `robots.txt` is a constant, and the sitemap lists
only rows the application already shows to a signed-out visitor. A URL that answered 404 to a
crawler would be a broken promise, so a login-walled page never appears here.
"""

from datetime import datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.seo import site_url
from app.models import Community, Post, User

router = APIRouter()

# One file is enough for a finite platform, and this cap keeps a runaway table from turning the
# answer into megabytes. Past it, the oldest rows stop being worth a crawl budget.
SITEMAP_LIMIT = 5000

# Prefixes a search engine should leave alone: everything behind the sign-in wall. `/login` and
# `/register` stay crawlable on purpose — they are where the public pages send a visitor.
DISALLOWED_PREFIXES = (
    "/dashboard",
    "/updates",
    "/conversations",
    "/notifications",
    "/people",
    "/account",
    "/calls",
    "/attachments",
    "/posts/new",
    "/communities/new",
    "/moderation",
    "/metrics",
    "/healthz",
    "/readyz",
)

# `robots.txt` has no notion of a path parameter, so the one walled page that hangs off a public
# prefix is excluded with the wildcard Google honours.
DISALLOWED_PATTERNS = ("/communities/*/reports",)

_CACHE_SECONDS = 3600


def _robots_body() -> str:
    lines = ["User-agent: *", "Allow: /"]
    lines.extend(f"Disallow: {prefix}" for prefix in DISALLOWED_PREFIXES)
    lines.extend(f"Disallow: {pattern}" for pattern in DISALLOWED_PATTERNS)
    # The sitemap is stated as an absolute address because the crawler resolves it against the
    # host it asked, and a deployment behind a proxy is not always the host in the setting.
    lines.extend(["", f"Sitemap: {site_url('/sitemap.xml')}", ""])
    return "\n".join(lines)


@router.get("/robots.txt", include_in_schema=False)
def robots() -> Response:
    return Response(
        content=_robots_body(),
        media_type="text/plain",
        headers={"Cache-Control": f"public, max-age={_CACHE_SECONDS}"},
    )


def _entry(location: str, last_modified: datetime | None = None, priority: str | None = None) -> str:
    parts = [f"    <loc>{escape(location)}</loc>"]
    if last_modified is not None:
        parts.append(f"    <lastmod>{last_modified.isoformat()}</lastmod>")
    if priority is not None:
        parts.append(f"    <priority>{priority}</priority>")
    return "  <url>\n" + "\n".join(parts) + "\n  </url>"


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap(database: Session = Depends(get_db)) -> Response:
    entries = [
        _entry(site_url("/"), priority="1.0"),
        _entry(site_url("/communities"), priority="0.8"),
    ]

    communities = database.execute(
        select(Community.id, Community.slug, Community.created_at)
        .where(Community.visibility == "public")
        .order_by(Community.created_at.desc())
        .limit(SITEMAP_LIMIT)
    ).all()
    entries.extend(
        _entry(site_url(f"/communities/{slug}"), created_at, "0.7")
        for _id, slug, created_at in communities
    )

    remaining = SITEMAP_LIMIT - len(entries)
    if remaining > 0:
        posts = database.execute(
            select(Community.slug, Post.id, Post.created_at)
            .join(Community, Community.id == Post.community_id)
            .join(User, User.id == Post.author_id)
            .where(
                Community.visibility == "public",
                Post.removed_at.is_(None),
                User.is_active.is_(True),
            )
            .order_by(Post.created_at.desc())
            .limit(remaining)
        ).all()
        entries.extend(
            _entry(site_url(f"/communities/{slug}/posts/{post_id}"), created_at, "0.6")
            for slug, post_id, created_at in posts
        )

    remaining = SITEMAP_LIMIT - len(entries)
    if remaining > 0:
        # A personal update is listed only when its author chose the open web. `everyone` is
        # still a signed-in audience, so it stays out of the sitemap and answers 404 here.
        updates = database.execute(
            select(Post.id, Post.created_at)
            .join(User, User.id == Post.author_id)
            .where(
                Post.community_id.is_(None),
                Post.audience == "public",
                Post.removed_at.is_(None),
                User.is_active.is_(True),
            )
            .order_by(Post.created_at.desc())
            .limit(remaining)
        ).all()
        entries.extend(
            _entry(site_url(f"/posts/{post_id}"), created_at, "0.5")
            for post_id, created_at in updates
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    return Response(
        content=body,
        media_type="application/xml",
        headers={"Cache-Control": f"public, max-age={_CACHE_SECONDS}"},
    )
