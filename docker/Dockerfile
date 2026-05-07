# syntax=docker/dockerfile:1.6
# gSage AI — Unified multi-target Dockerfile.
#
# One file replaces the legacy per-service Dockerfiles under
# docker/{backend_api,celery,email_worker,mcp_server,dev}/.
#
# Build targets:
#   base              — python + build deps + pip install requirements.txt
#                       (internal; not meant to be used directly)
#   runtime-minimal   — email_worker: lean runtime
#   runtime-api       — backend_api: runtime-minimal + chromium + node20 +
#                       mermaid-cli (used by the Teams handler to render
#                       Mermaid diagrams as PNG attachments inline)
#   runtime-tools     — celery-worker-tools: tshark, nmap, whatweb, pandoc,
#                       texlive (for report generation)
#   runtime-mermaid   — mcp_server: chromium + node20 + mermaid-cli
#                       (for the mermaid_validate tool)
#   dev               — superset of every runtime target plus git. Used by
#                       docker-compose in local development, where source
#                       code is bind-mounted and `pip install -r` runs at
#                       container startup.
#
# Build examples (run from project root):
#   docker build -f docker/Dockerfile --target runtime-minimal -t gsage-email_worker .
#   docker build -f docker/Dockerfile --target runtime-api     -t gsage-backend_api .
#   docker build -f docker/Dockerfile --target runtime-tools   -t gsage-celery-tools .
#   docker build -f docker/Dockerfile --target runtime-mermaid -t gsage-mcp-server   .
#   docker build -f docker/Dockerfile --target dev             -t gsage-python-dev-image .

# =============================================================================
# Stage 1 — builder: compile Python wheels once, share across runtime stages
# =============================================================================
FROM python:3.12-slim AS builder

# unixodbc-dev: required to compile pyodbc (dep of aioodbc / SQL Server support)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc python3-dev libffi-dev libssl-dev libpq-dev \
        unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# =============================================================================
# Stage 2 — base: minimal runtime with Python deps and appuser
#
# IMPORTANT: this stage MUST NOT copy application source code. All `COPY src/`
# and similar steps are performed at the END of each runtime-* stage, AFTER
# the heavy `apt-get install` layers. This keeps the apt caches valid across
# rebuilds triggered by source code changes — otherwise editing any file under
# src/ would invalidate `base` and force tshark/texlive/chromium/nodejs to be
# reinstalled in every downstream stage.
# =============================================================================
FROM python:3.12-slim AS base

WORKDIR /app

# Copy pre-compiled site-packages from builder
COPY --from=builder /install /usr/local

# Runtime ODBC library + curl for healthchecks + non-root user.
# unixodbc: required by pyodbc at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl unixodbc \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r appuser && useradd -r -g appuser -s /sbin/nologin appuser \
    && mkdir -p /data/ingest \
    && chown -R appuser:appuser /data

# ── Optional: Microsoft ODBC Driver 18 for SQL Server ────────────────────────
# Uncomment the block below to enable SQL Server connectivity (msodbcsql18).
# Required when using the sql_query tool with SQL Server targets.
# Reference: https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server
#
# RUN curl -sSL https://packages.microsoft.com/keys/microsoft.asc \
#         | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
#     && curl -sSL https://packages.microsoft.com/config/debian/12/prod.list \
#         > /etc/apt/sources.list.d/mssql-release.list \
#     && apt-get update \
#     && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
#     && rm -rf /var/lib/apt/lists/*

# =============================================================================
# Stage 3a — runtime-minimal: backend_api, email_worker
# =============================================================================
FROM base AS runtime-minimal

# Application source — copied LAST so source-only changes don't bust the
# layer cache of the (much heavier) apt installs in sibling stages.
# In dev compose these paths are overridden by bind mounts.
COPY src/ /app/src/
COPY requirements.txt /app/requirements.txt
COPY alembic.ini /app/alembic.ini
COPY scripts/ /app/scripts/

USER appuser
EXPOSE 8000
# Default command; override per service in docker-compose.
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", \
     "src.backend_api.app.main:app", "--bind", "0.0.0.0:8000"]

# =============================================================================
# Stage 3a-bis — runtime-api: backend_api with mermaid-cli for Teams handler
#
# The Teams channel cannot render Mermaid blocks natively, so the backend
# pre-processes agent responses, renders ```mermaid blocks via mmdc and
# attaches the PNG inline as a Bot Framework attachment. This stage adds
# chromium + node20 + @mermaid-js/mermaid-cli on top of runtime-minimal's
# layout. The Mermaid renderer module is shared with the mcp_server
# (src/shared/services/mermaid_renderer.py).
# =============================================================================
FROM base AS runtime-api

# chromium + nodejs 20 + mermaid-cli (same setup as runtime-mermaid).
# PUPPETEER_SKIP_DOWNLOAD=1 reuses the system chromium instead of letting
# Puppeteer download its own bundled copy.
RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && apt-get install -y --no-install-recommends \
        gnupg ca-certificates chromium fonts-liberation fonts-noto-color-emoji \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && PUPPETEER_SKIP_DOWNLOAD=1 npm install -g --omit=dev \
        @mermaid-js/mermaid-cli@11.4.2 \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/* /root/.npm

ENV PUPPETEER_SKIP_DOWNLOAD=1 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# Application source — copied LAST (see runtime-minimal note).
COPY src/ /app/src/
COPY requirements.txt /app/requirements.txt
COPY alembic.ini /app/alembic.ini
COPY scripts/ /app/scripts/

USER appuser
EXPOSE 8000
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", \
     "src.backend_api.app.main:app", "--bind", "0.0.0.0:8000"]

# =============================================================================
# Stage 3b — runtime-tools: celery-worker-tools (network/security tools,
#                           pandoc + texlive for report generation,
#                           mermaid-cli + graphviz for diagram rendering
#                           inside generate_document via the diagram.lua
#                           Lua filter shipped in the pandoc_gsage bundle)
# =============================================================================
FROM base AS runtime-tools

# tshark: non-interactive install (disable setuid prompt for wireshark-common).
# chromium + nodejs + mermaid-cli: required by the diagram.lua filter (mermaid
# engine) used by generate_document. Same flags as runtime-mermaid: skip the
# bundled Puppeteer Chromium download and reuse the system one.
# graphviz: required by the diagram.lua filter (dot engine).
RUN echo "wireshark-common wireshark-common/install-setuid boolean false" \
        | debconf-set-selections \
    && DEBIAN_FRONTEND=noninteractive apt-get update \
    && apt-get install -y --no-install-recommends \
        gnupg nmap tshark whatweb pandoc graphviz \
        ca-certificates chromium fonts-liberation fonts-noto-color-emoji \
        texlive-latex-base texlive-latex-recommended texlive-latex-extra \
        texlive-base texlive-fonts-recommended texlive-fonts-extra lmodern fonts-dejavu \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && PUPPETEER_SKIP_DOWNLOAD=1 npm install -g --omit=dev \
        @mermaid-js/mermaid-cli@11.4.2 \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/* /root/.npm

ENV PUPPETEER_SKIP_DOWNLOAD=1 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# Application source — copied LAST (see runtime-minimal note).
COPY src/ /app/src/
COPY requirements.txt /app/requirements.txt
COPY alembic.ini /app/alembic.ini
COPY scripts/ /app/scripts/

USER appuser
# Default command; override per service/queue in docker-compose.
CMD ["celery", "-A", "src.backend_api.app.celery_app", "worker", \
     "--queues=tools", "--loglevel=info"]

# =============================================================================
# Stage 3c — runtime-mermaid: mcp_server (mermaid_validate tool)
# =============================================================================
FROM base AS runtime-mermaid

# nodejs 20 + @mermaid-js/mermaid-cli + headless chromium.
# Puppeteer is configured via PUPPETEER_SKIP_DOWNLOAD=1 to reuse the system
# chromium rather than downloading its own bundled copy.
# tshark is also included here because mcp_server tools may run network
# analysis directly (e.g. pcap_analyzer) without delegating to celery.
RUN echo "wireshark-common wireshark-common/install-setuid boolean false" \
        | debconf-set-selections \
    && DEBIAN_FRONTEND=noninteractive apt-get update \
    && apt-get install -y --no-install-recommends \
        gnupg tshark \
        ca-certificates chromium fonts-liberation fonts-noto-color-emoji \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && PUPPETEER_SKIP_DOWNLOAD=1 npm install -g --omit=dev \
        @mermaid-js/mermaid-cli@11.4.2 \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/* /root/.npm

ENV PUPPETEER_SKIP_DOWNLOAD=1 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# Application source — copied LAST (see runtime-minimal note).
COPY src/ /app/src/
COPY requirements.txt /app/requirements.txt
COPY alembic.ini /app/alembic.ini
COPY scripts/ /app/scripts/

USER appuser
EXPOSE 8001
CMD ["uvicorn", "src.mcp_server.main:app", "--host", "0.0.0.0", "--port", "8001"]

# =============================================================================
# Stage 3d — dev: superset for local development (bind-mounted source)
#
# Includes every apt package from runtime-tools + runtime-mermaid, plus git and
# build toolchain (kept here so `pip install` at container startup can compile
# C extensions for packages not pinned as wheels). Runs as root to allow the
# compose command `pip install --quiet -r requirements.txt` at startup.
# =============================================================================
FROM base AS dev

# Keep the builder toolchain so container-time `pip install` can compile
# wheels if a new dependency is added without an image rebuild.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc python3-dev libffi-dev libssl-dev libpq-dev \
        unixodbc-dev git gnupg nmap pandoc graphviz \
        texlive-latex-base texlive-latex-recommended texlive-latex-extra \
        texlive-base texlive-fonts-recommended texlive-fonts-extra lmodern fonts-dejavu \
    && echo "wireshark-common wireshark-common/install-setuid boolean false" \
        | debconf-set-selections \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tshark whatweb \
    && apt-get install -y --no-install-recommends \
        ca-certificates chromium fonts-liberation fonts-noto-color-emoji \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && PUPPETEER_SKIP_DOWNLOAD=1 npm install -g --omit=dev \
        @mermaid-js/mermaid-cli@11.4.2 \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/* /root/.npm

ENV PUPPETEER_SKIP_DOWNLOAD=1 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# Dev runs as root: source code is bind-mounted and the compose command
# performs `pip install -r requirements.txt` at startup.
# No CMD: each service in docker-compose.yml supplies its own command.
