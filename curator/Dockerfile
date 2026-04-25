# Curator — Reputation Lists Service
# Build: docker build -t curator -f curator/Dockerfile .
#
# Production multi-stage image.
# Development: use gsage-python-dev-image + volume mounts (see docker-compose.yml).

FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc python3-dev libffi-dev libssl-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY curator/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ────────────────────────────────────────────────────────────

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app
COPY curator/app /app/app

RUN groupadd -r curator \
    && useradd -r -g curator -m -d /home/curator -s /usr/sbin/nologin curator
RUN mkdir -p /data && chown curator:curator /data
ENV HOME=/home/curator
USER curator

CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "app.main:app", "--bind", "0.0.0.0:8000", "--workers", "2"]
