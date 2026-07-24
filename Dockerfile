# ── Build stage ─────────────────────────────────────────────
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Runtime ─────────────────────────────────────────────────
COPY main.py config.py database.py google_auth.py sync.py middleware.py ./

# Non-root user + dedicated data dir for the SQLite DB
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app
USER appuser

# DB lives on a mounted volume so it survives container restarts
ENV DATABASE_PATH=/data/members.db \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import httpx,os; httpx.get(f\"http://127.0.0.1:{os.getenv('PORT','8000')}/health\", timeout=4).raise_for_status()"

# Single worker only — SQLite + shared in-process connection cannot scale horizontally
CMD ["sh", "-c", "uvicorn main:app --host ${HOST} --port ${PORT} --workers 1"]
