# gSage SOC AI — Agent Guide

> This file is written for AI coding agents. It assumes no prior knowledge of the project and focuses on the conventions, architecture, and commands you need to work safely and effectively in this codebase.

## Project overview

**gSage SOC AI** is an on-premise SOC assistant that combines AI agents, structured tooling, and human-review workflows to help security teams monitor, investigate, triage, and respond faster. The platform is designed for organizations that want AI-assisted security operations without sending core workflows or operational data to a third-party SaaS by default.

Key traits:

- **On-premise first** — runs as a Docker Compose stack on a Linux host.
- **Multi-tenant by design** — organizations, departments, users, groups, and permission tags are part of the core model.
- **Audit-friendly** — traces, tool executions, and agent runs are logged to Elasticsearch.
- **Human-in-the-loop** — tool execution can require approval through configurable rules.
- **Multiple interfaces** — web UI, Telegram, email, and Microsoft Teams.

Version: `0.6.0` (see `VERSION`).

## Technology stack

| Layer | Technology |
|-------|------------|
| Runtime | Python 3.12, Node 22 (build only) |
| Backend API | FastAPI, SQLAlchemy 2.0 (async), Pydantic v2, PyJWT |
| Agent framework | Agno (`agno>=0.1`) |
| Tool protocol | MCP (Model Context Protocol), streamable HTTP transport |
| Background jobs | Celery + RedBeat, Redis 7 as broker/backend |
| Primary database | PostgreSQL 16 |
| Migrations | Alembic (`src/migrations/`) |
| Vector database | Weaviate 1.28.3 (`text2vec-ollama`) |
| Search / audit logs | Elasticsearch 8.13 + Kibana 8.13 |
| Object storage | MinIO |
| Local LLM / embeddings | Ollama |
| Other LLM providers | OpenAI, DeepSeek, Anthropic, Google Gemini, vLLM-compatible endpoints |
| Frontend | React 18 + Vite 6 + TypeScript 5 + Tailwind CSS 3 + Radix UI |
| State / data fetching | TanStack Query v5, React Hook Form, React Router v7 |
| Terminal UIs | Textual (`admin_console/`), Rich (`cli_client/`) |
| Reputation lists | Curator microservice (`curator/`) |

## Architecture and service topology

All services attach to the Docker bridge network `gsage-internal`. The public surface is a single port (default `8080`) served by the `frontend` nginx container; the backend is reached through the reverse proxy at `/api`.

```
Users / Analysts
   ├─ Web browser ──────────────► frontend (nginx) ──► backend_api:8000/api
   ├─ Telegram ─────────────────► telegram-worker
   ├─ Email (IMAP/SMTP) ────────► email-worker ──────► Celery
   └─ Microsoft Teams ──────────► backend_api Bot Framework webhook

backend_api (FastAPI)
   ├─ PostgreSQL (SQLAlchemy + Alembic)
   ├─ Redis (cache, broker, locks, pub/sub)
   ├─ Weaviate (knowledge base vectors)
   ├─ Elasticsearch (audit/traces)
   ├─ MinIO (files)
   ├─ Celery workers (tools, email, scheduled, knowledge, elasticsearch)
   ├─ Celery Beat (RedBeat scheduler)
   └─ MCP Server (streamable HTTP) ──► tool registry + custom tools

Curator (FastAPI) ──► PostgreSQL curator DB (reputation lists)
Ollama ──► LLM + embeddings
```

Communication patterns:

- **HTTP/REST** — backend ↔ web UI, backend ↔ MCP server, backend ↔ curator.
- **SSE** — streaming chat responses from `/api/v1/chat/stream`.
- **MCP streamable HTTP** — backend calls the MCP server at `http://mcp-server:8001`. Tenant identity travels in headers: `X-Organization-ID`, `X-User-ID`, `X-Org-Role`.
- **Celery + Redis** — task dispatch across queues: `celery`, `tools`, `email`, `scheduled`, `knowledge`, `elasticsearch`.
- **Weaviate gRPC/REST** — semantic search for the knowledge base.
- **Elasticsearch** — buffered audit traces.
- **MinIO** — S3-compatible internal object storage.

## Code organization

```
admin_console/          Textual-based operator/admin TUI
cli_client/             Rich-based terminal chat client
 curator/                FastAPI reputation/allowlist/blocklist service
 custom_code/            Supported extension point for operator tools/auth backends
   auth_backends/
   tools/
   tests/
docker/                 Dockerfiles and datastore init scripts
 docs/                   User-facing and developer documentation
 docs-local/             Architecture docs (symlinked to external repo)
 installer/              Production installer, compose files, wizard
 scripts/                Helper scripts (init ES, docs generation, etc.)
 scripts_operations/     Build, publish, cleanup, diagnostic scripts
 src/
   backend_api/app/      FastAPI control plane: routers, services, tasks, core
   email_worker/         IMAP IDLE/polling email channel
   mcp_server/           MCP tool execution plane
   migrations/           Alembic migrations
   ops_cli/              Operational CLI (channels, auth-providers)
   shared/               Shared libraries: auth, models, security, config, cache, LLM, logging
   teams_handler/        Microsoft Teams channel adapter
   telegram_worker/      Telegram bot worker
 tests/                  pytest suite (unit/, integration/)
 web_client/             React SPA
```

Key backend modules:

- `src/backend_api/app/api/v1/router.py` — mounts all API routers.
- `src/backend_api/app/services/agent_factory.py` — builds Agno agents and wires MCP tools.
- `src/backend_api/app/services/knowledge.py` / `knowledge_tools.py` — Weaviate KB operations.
- `src/backend_api/app/services/elasticsearch.py` — trace/audit logging.
- `src/backend_api/app/services/approval_delegations.py` / `tool_auto_approve.py` — HITL approval logic.
- `src/backend_api/app/tasks/*.py` — Celery task definitions.
- `src/mcp_server/tools/base.py` — `BaseTool` execution contract.
- `src/mcp_server/registry/registry.py` — tool discovery and registration.
- `src/shared/security/permissions.py` — permission model.
- `src/shared/models/` — SQLAlchemy models (prefix `GSage*`, e.g. `GSageUser`, `GSageOrganization`).

## Development setup

The intended development workflow uses Docker Compose with bind-mounted source and the `gsage-python-dev-image`.

```bash
# 1. Copy and edit environment
cp .env.example .env
# Edit .env to set LLM_PROVIDER, database passwords, and any tool API keys.

# 2. Build the dev image and start the stack
bash scripts_operations/build-dev-image.sh
docker compose up -d

# 3. Run database migrations
docker compose exec backend alembic upgrade head

# 4. Initialize Elasticsearch indices/templates
docker compose exec backend python scripts/init-elasticsearch.py

# 5. Retrieve the bootstrap admin API key from the backend logs
docker compose logs backend | grep 'Admin API Key'
```

Important `.env` variables to review:

- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
- `REDIS_HOST=redis`, `REDIS_PASSWORD`
- `LLM_PROVIDER` — `ollama`, `vllm`, `openai`, `deepseek`, `anthropic`, `gemini`
- Provider endpoints: `OLLAMA_BASE_URL`, `VLLM_BASE_URL`, `OPENAI_BASE_URL`, etc.
- Tool credentials such as `VT_API_KEY`, `ABUSEIPDB_API_KEY`, etc.

## Build and run commands

### Local development stack

```bash
bash scripts_operations/build-dev-image.sh   # build gsage-python-dev-image
docker compose up -d                           # start all services
docker compose logs -f backend                 # tail backend logs
docker compose exec backend alembic upgrade head
```

### Frontend only

```bash
cd web_client
npm install
npm run dev          # Vite dev server, default http://localhost:5173
npm run build        # production build
npm run lint         # ESLint
```

### CLI client

```bash
pip install -r requirements-cli.txt
export GSAGE_API_KEY='your-api-key-here'
export GSAGE_API_HOST='http://localhost:8080'  # optional
python -m cli_client.main
# or
./run-cli.sh
```

### Admin console

```bash
./run-admin.sh
```

### Operational CLI

```bash
python -m ops_cli --help
python -m ops_cli channels --help
python -m ops_cli auth-providers --help
```

## Testing instructions

Tests use `pytest` with `pytest-asyncio`.

```bash
# all tests
pytest

# by marker
pytest -m unit
pytest -m integration
pytest -m security
pytest -m fuzz

# specific file
pytest tests/integration/test_rate_limiting.py -v
```

Test markers (defined in `pytest.ini`):

- `unit` — no external dependencies.
- `integration` — in-process HTTP tests against the FastAPI app with mocked DB/Redis.
- `security` — security-focused tests.
- `fuzz` — fuzz tests for tools.

Shared fixtures live in `tests/conftest.py`. Existing test files:

- `tests/unit/shared/test_response_filter.py`
- `tests/integration/test_api_key_isolation.py`
- `tests/integration/test_rate_limiting.py`
- `tests/integration/test_tenant_isolation.py`

> Do **not** run the full test suite automatically in agent workflows. Indicate the command for the user to run.

## Code style guidelines

The project does not enforce an automated formatter/linter at the repository root. The closest style authority is `.github/copilot-instructions.md`. Conventions observed in the codebase:

### Language

- **Technical documentation and code comments must be in English.**
- Existing Portuguese docs should be translated to English when updated.

### Imports

Use this order, starting with `from __future__ import annotations`:

1. `from __future__ import annotations`
2. Standard library
3. Third-party packages
4. Local `src.*` absolute imports

Example:

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import redis.asyncio as redis
from fastapi import FastAPI

from src.shared.config.settings import get_settings
```

### Naming conventions

- Files: `snake_case.py`
- Classes: `PascalCase`
- Functions/methods/variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Database models: prefixed with `GSage` (e.g. `GSageUser`, `GSageOrganization`, `GSageToolConfig`)
- Private helpers: leading underscore (e.g. `_build_agent_context`)
- Alembic migrations: `YYYYMMDD_HHMM_<description>.py`

### Code patterns

- **Async-first** — prefer `async def`; SQLAlchemy uses `AsyncSession`.
- **SQLAlchemy 2.0** — use `Mapped[...]`, `mapped_column(...)`, `select(...)`.
- **Pydantic v2** — use `pydantic_settings.BaseSettings`, `model_validator`.
- **Type annotations** — annotate public functions and classes.
- **Logging** — use `logging.getLogger(__name__)`.
- **Multi-tenant isolation** — every query, endpoint, and service must respect `org_id` and department scoping.
- **Environment variables** — whenever you add a new env var, add it to `.env.example` with a descriptive comment and example value, and tell the user to update their `.env`.
- **Frontend i18n** — in `web_client/`, ensure every new user-facing text key is added to `translations.json`.

### Type checking

`pyrightconfig.json` points to `.venv`. Excluded directories: `.venv`, `dist`, `external_code`, `limbo`, `**/node_modules`.

## Security considerations

- **Authentication** — JWT access/refresh tokens (`PyJWT`). Supports local, LDAP/AD, and Microsoft Entra OIDC backends.
- **Authorization** — permission-based tool access via groups and permission tags.
- **Tenant isolation** — `org_id` must be enforced in every query, endpoint, and service. Tenant context is resolved in `src/backend_api/app/core/tenant.py` and passed to the MCP server via headers.
- **Credential encryption** — sensitive tool credentials are encrypted with AES-256-GCM before storage.
- **Tool audit logging** — every tool execution is audited through `ToolAuditLogger` and traced to Elasticsearch.
- **Human approval** — `approval_delegations.py` and `tool_auto_approve.py` implement configurable rules for approving/rejecting tool execution.
- **Rate limiting** — org-scoped routes are rate-limited; SSE stream routes are authenticated but not rate-limited.
- **Sensitive files** — do not commit `.env`, secrets, or credential files.

## Deployment process

### Docker images

The unified `docker/Dockerfile` has multiple targets:

| Target | Used by | Extras |
|--------|---------|--------|
| `runtime-minimal` | email worker, lean backend | base runtime |
| `runtime-api` | `backend_api` | Chromium + Node 20 + mermaid-cli |
| `runtime-tools` | `celery-worker-tools` | nmap, tshark, whatweb, pandoc, texlive, graphviz |
| `runtime-mermaid` | `mcp-server` | Chromium + Node 20 + mermaid-cli |
| `dev` | local development | superset of all targets + git |

Other images:

- `web_client/Dockerfile` — Node 22 build → nginx 1.27.
- `curator/Dockerfile` — standalone Python 3.12 FastAPI service.

### Compose files

- `docker-compose.yml` — development stack with bind mounts and many exposed ports.
- `installer/compose/docker-compose.prod.yml` — production stack pulling pre-built registry images, no bind mounts, stricter resource limits.

### Release flow

```bash
# 1. Build and push runtime images
bash scripts_operations/publish-images.sh \
  --registry docker.io/guardiankey \
  --tag 0.6.0 \
  --push

# 2. Build release bundle
bash scripts_operations/build-release-bundle.sh \
  --version 0.6.0 \
  --registry docker.io/guardiankey

# 3. Install on the target host
curl -fsSL https://raw.githubusercontent.com/guardiankey/gsage-ai-soc/main/dist/get-gsage.sh | sudo bash
# or after manual download:
sudo bash installer/installer.sh
```

The installer:

- runs preflight checks (root, OS/arch, Docker, Python, RAM/disk, ports),
- asks for admin credentials, host port, and LLM provider,
- writes a `0600` `.env` to `/opt/gsage/shared/.env` with generated secrets,
- brings up the stack via `docker compose`,
- waits for `backend_api` health,
- installs host wrappers into `/usr/local/bin`: `gsage-cli`, `gsage-admin`, `gsage-get-admin-key`.

Re-running the installer upgrades in place while preserving `.env` and volumes.

### Operational helpers

Useful scripts in `scripts_operations/`:

- `build-dev-image.sh` / `rebuild-backend.sh`
- `clean-db.sh`, `clean-redis-cache.sh`, `clean_migrations.sh`
- `recreate-postgresql.sh`, `recreate-redis.sh`, `recreate-weaviate.sh`, `recreate-elasticsearch.sh`, `recreate-minio.sh`
- `debug_scheduled_jobs.py`, `test_elasticsearch.py`, `test_setup.py`
- `publish-images.sh`, `build-release-bundle.sh`

## Extension points

### Adding a tool

See `docs/dev/TOOLS.md`.

Short version:

1. Create a `BaseTool` subclass under `custom_code/tools/`.
2. Define `name`, `permissions`, `params_schema`, and `execute()`.
3. Optionally define config, state, approval, background, or multi-profile behavior.
4. Restart the MCP server so discovery runs.
5. Assign the generated permission tags to a group and configure tool profiles for the organization.

### Adding an authentication provider

See `docs/dev/AUTH_PROVIDERS.md`.

Short version:

1. Create a `BaseAuthProvider` subclass under `custom_code/auth_backends/`.
2. Implement `authenticate()` and return a correct `AuthResult`.
3. Define `config_defaults` and `config_schema`.
4. Add the provider name to the organization's `auth_providers` chain.
5. Configure the provider in the organization's encrypted `auth_config` payload.

## Agent workflow rules

The following rules come from `.github/copilot-instructions.md` and apply to agent operations:

- Do **not** restart containers or services automatically. Alert the user and indicate the commands.
- Do **not** create Alembic migrations by hand-editing `.py` files. In the development venv you may run:
  - `alembic revision --autogenerate -m "appropriate comment"`
  - `alembic upgrade head`
- Do **not** run `docker compose up/down/restart`, `git push`, `git reset --hard`, `rm -rf`, or similar without user confirmation.
- Use service names with `docker compose exec` (e.g. `docker compose exec backend bash`), not container names.
- When design choices exist, present options and ask the user before proceeding.
- Prefer existing frameworks/libraries over custom solutions.
- Verify Pylance errors before finishing.
- Update `docs-local/architecture/` (one `.md` per feature) and `docs/CLI_CLIENT.md` after implementation.
- Suggest tests for new functionality, but do not implement them without user request.
- Do not run the full test suite automatically; provide the command instead.

## Useful references

- `README.md` — project overview, install, and usage.
- `docs/dev/README.md` — developer-oriented architecture.
- `docs/dev/TOOLS.md` — tool execution model and how to add tools.
- `docs/dev/AUTH_PROVIDERS.md` — adding authentication providers.
- `docs/tools/README.md` — existing tool documentation.
- `.env.example` — reference for all environment variables.
- `.github/copilot-instructions.md` — project-specific coding rules (Portuguese text, but mandates English technical docs and code comments).
