# syntax=docker/dockerfile:1

# ---------- build stage: resolve and compile dependencies into a venv ----------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && find /opt/venv -name "__pycache__" -type d -prune -exec rm -rf {} + \
 && find /opt/venv -name "tests" -path "*site-packages/*/tests" -type d -prune -exec rm -rf {} +

# ---------- runtime stage: slim image with only the venv and app code ----------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DATABASE_URL=sqlite:////data/app.db \
    PORT=8000
RUN groupadd --system app && useradd --system --gid app --home /app app \
 && mkdir -p /data && chown app:app /data
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
# /data is provided by a mounted volume (docker-compose named volume / Railway volume).
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4)" || exit 1
STOPSIGNAL SIGTERM
ENTRYPOINT ["docker-entrypoint.sh"]
# Single worker: SQLite + in-process LiteLLM cache are per-process by design.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 20"]
