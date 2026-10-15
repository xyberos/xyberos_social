# Application image for the beta deployment.
#
# The same requirements.txt that CI tests is installed here, so the image is the tested
# environment rather than a second one; pytest and ruff ride along because the plan treats
# test evidence as part of the release, and a beta image is not size-optimized yet.
#
# One worker on purpose: WebSocket fan-out is process-local, so a second worker would split
# conversations across processes and drop live messages for viewers connected to the other
# one. Raise workers only after shared fan-out exists (Phase 4 caveat, Phase 7 review).

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend

WORKDIR /app

# Dependencies are copied and installed first so editing application code does not
# invalidate the installed layers.
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install --requirement requirements.txt

COPY backend/ ./backend/

# Run as an unprivileged user; nothing in the container needs to write to the image.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && chown --recursive appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

# Forwarded headers stay untrusted unless the deployment sets --forwarded-allow-ips to its
# proxy address, because the rate limiter and the request log key on the client address.
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
