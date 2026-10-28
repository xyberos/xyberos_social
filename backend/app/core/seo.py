"""Search-engine metadata shared by the page templates and the crawl files.

A canonical URL is the path alone. Filters, cursors, a message banner, and a page number in
`?query` change which rows a page shows, never what the page is, so every variant names one
address and a crawler never treats two of them as two documents.
"""

from fastapi import Request

from app.core.config import get_settings

SITE_NAME = "Social+"

DEFAULT_DESCRIPTION = (
    "Social+ is a calmer social network: follow the people and communities you choose, "
    "then read a finite feed instead of an endless one."
)

# Long enough for a useful snippet, short enough that a search engine shows it instead of
# rewriting one of its own.
DESCRIPTION_LIMIT = 160


def site_url(path: str = "/") -> str:
    """Turn an application path into the absolute address a crawler can follow."""
    return f"{get_settings().app_base_url.rstrip('/')}{path}"


def canonical_url(request: Request) -> str:
    return site_url(request.url.path)


def describe(
    text: str | None,
    fallback: str = DEFAULT_DESCRIPTION,
    limit: int = DESCRIPTION_LIMIT,
) -> str:
    """A one-line summary for a meta tag: collapsed whitespace, bounded length."""
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return fallback
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"
