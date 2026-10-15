"""Application assembly: logging, middleware, routers, and the ASGI entry point."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.middleware import register_middleware
from app.api.responses import BASE_DIR
from app.api.router import api_router
from app.core.config import get_settings
from app.core.observability import configure_logging

configure_logging(get_settings().json_logs)

app = FastAPI(title="Xyberos Social", version="0.1.0")
register_middleware(app)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(api_router)
