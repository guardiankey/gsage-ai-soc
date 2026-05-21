# syntax=docker/dockerfile:1.6
# Curator — Reputation Lists Service
# Build: docker build -t curator -f curator/Dockerfile .
#
# Production multi-stage image.
# Development: use gsage-python-dev-image + volume mounts (see docker-compose.yml).

FROM python:3.12-slim AS builder

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc python3-dev libffi-dev libssl-dev libpq-dev

WORKDIR /build
COPY curator/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --prefix=/install -r requirements.txt

# ── Runtime stage ────────────────────────────────────────────────────────────

FROM python:3.12-slim

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl

COPY --from=builder /install /usr/local

WORKDIR /app
COPY curator/app /app/app

RUN groupadd -r curator \
    && useradd -r -g curator -m -d /home/curator -s /usr/sbin/nologin curator
RUN mkdir -p /data && chown curator:curator /data
ENV HOME=/home/curator
USER curator

CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "app.main:app", "--bind", "0.0.0.0:8000", "--workers", "2"]
