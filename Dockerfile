# WikiRyvals backend - single-worker FastAPI image with the prebuilt snapshot
# baked in, so the container runs immediately with no crawl.
FROM python:3.12-slim

# Durable state (accounts + play graph) defaults onto /app/pgdata so a single
# mounted volume survives restarts/redeploys. Override any of these at runtime.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WIKIRYVALS_PLAY_GRAPH=/app/pgdata/play_graph.sqlite3 \
    WIKIRYVALS_ACCOUNTS=/app/pgdata/accounts.sqlite3

WORKDIR /app

# Install dependencies first (editable install keeps the package rooted at /app so
# wikirace's DATA_DIR resolves to /app/data, where the baked snapshot lives).
COPY pyproject.toml README.md ./
COPY wikirace ./wikirace
COPY snapshot ./snapshot
COPY data ./data

# Run as a non-root user; pre-create the state dir and hand it to that user so the
# named volume inherits writable ownership on first mount.
RUN pip install -e . \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/pgdata \
    && chown -R app:app /app/pgdata /app/data
USER app

EXPOSE 8011

# Container-native healthcheck (compose/orchestrators can also override this).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8011/api/ext/health').status==200 else 1)"

# One worker on purpose: race + graph state lives in-process, so multiple workers
# would need a shared store (e.g. Redis) - see README "Scaling" before bumping it.
CMD ["uvicorn", "wikirace.app:app", "--host", "0.0.0.0", "--port", "8011"]
