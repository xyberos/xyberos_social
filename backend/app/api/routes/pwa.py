"""The web app manifest, so the application can be installed to a home screen.

Served from a route rather than a static file because `StaticFiles` guesses the content type
from the extension, and a manifest with the wrong type is ignored. `start_url` is the dashboard:
an installed app is one a signed-in person added, and the sign-in redirect covers the rest.
"""

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from app.core.seo import DEFAULT_DESCRIPTION, SITE_NAME

router = APIRouter()

# A manifest changes only when this file does, so a short shared lifetime is enough and keeps
# an icon change from waiting on a browser cache that never revalidates.
_CACHE_SECONDS = 3600

_ICONS = (
    ("/static/icons/icon-192.png", "192x192", "any"),
    ("/static/icons/icon-512.png", "512x512", "any"),
    ("/static/icons/icon-maskable-512.png", "512x512", "maskable"),
)


def manifest_document() -> dict:
    return {
        "id": "/",
        "name": f"{SITE_NAME} — a calmer social network",
        "short_name": SITE_NAME,
        "description": DEFAULT_DESCRIPTION,
        "start_url": "/dashboard",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#f3f5ef",
        "theme_color": "#245b48",
        "lang": "en",
        "dir": "ltr",
        "categories": ["social"],
        "icons": [
            {"src": src, "sizes": sizes, "type": "image/png", "purpose": purpose}
            for src, sizes, purpose in _ICONS
        ],
    }


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> Response:
    return JSONResponse(
        manifest_document(),
        media_type="application/manifest+json",
        headers={"Cache-Control": f"public, max-age={_CACHE_SECONDS}"},
    )
