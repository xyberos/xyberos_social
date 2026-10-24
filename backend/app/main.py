"""Application assembly: logging, middleware, routers, and the ASGI entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.middleware import register_middleware
from app.api.responses import BASE_DIR
from app.api.router import api_router
from app.core.config import get_settings
from app.core.observability import configure_logging
from app.core.storage import StorageError, bootstrap_bucket

configure_logging(get_settings().json_logs)

logger = logging.getLogger(__name__)

# How long development startup waits for object storage before serving anyway.
BOOTSTRAP_TIMEOUT_SECONDS = 5


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Prepare local object storage.

    The development stand-in keeps its state in memory, so the bucket disappears the moment it
    restarts and every upload then fails with `NoSuchBucket`. Recreating it here costs one
    request and keeps a local stack usable after a container restart. Production stays an
    operator step: creating and re-policing a real bucket on every boot is not something a
    request process should decide. The call is bounded, so a storage service that is down
    delays startup by seconds at most.
    """
    if get_settings().app_env == "development":
        try:
            # Bounded on purpose: a storage service that is down or unresponsive must cost a
            # warning at boot, never a startup that hangs. The work continues in the thread and
            # its result stops mattering, because the first upload asks again.
            await asyncio.wait_for(
                asyncio.to_thread(bootstrap_bucket), timeout=BOOTSTRAP_TIMEOUT_SECONDS
            )
        except (StorageError, TimeoutError) as error:
            logger.warning(
                "could not prepare the development bucket",
                extra={"event": "storage.bootstrap_failed", "error": str(error)},
            )
    yield


app = FastAPI(title="Xyberos Social", version="0.1.0", lifespan=lifespan)
register_middleware(app)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(api_router)
