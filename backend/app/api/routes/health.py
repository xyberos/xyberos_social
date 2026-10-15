"""Liveness and readiness probes."""

import logging
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from app.core.database import get_db

router = APIRouter()

logger = logging.getLogger(__name__)


@router.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness only: no dependencies, so a database outage never triggers a restart."""
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readyz(database: Session = Depends(get_db)):
    try:
        database.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("readiness check failed", extra={"event": "readiness.failed"})
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}
