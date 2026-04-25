# gSage AI Developer Documentation

This folder documents the current extension and integration surfaces of the project.
The source of truth is the codebase under `src/`, `custom_code/`, and the active
runtime topology in `docker-compose.yml`.

If this documentation and the code ever disagree, trust the code and update the docs.

---

## Fast Paths

### I want to add a tool

Read `TOOLS.md`.

The short version is:

1. Create a concrete `BaseTool` subclass under `custom_code/tools/`.
2. Define `name`, `permissions`, `params_schema`, and `execute()`.
3. Optionally define config, state, approval, background, or multi-profile behavior.
4. Restart the MCP server so discovery runs.
5. Assign the generated permission tags to a group and configure tool profiles for the organization.

### I want to add an authentication provider

Read `AUTH_PROVIDERS.md`.

The short version is:

1. Create a concrete `BaseAuthProvider` subclass under `custom_code/auth_backends/`.
2. Implement `authenticate()` and return a correct `AuthResult`.
3. Define `config_defaults` and `config_schema` for deployment and per-org config.
4. Add the provider name to the organization's `auth_providers` chain.
5. Configure the provider in the organization's encrypted `auth_config` payload.

---

## Core Concepts In Two Minutes

1. `src/backend_api/app/` is the control plane.
   It owns authentication, JWTs, org/user data, agent orchestration, approvals, background tasks, and public APIs.

2. `src/mcp_server/` is the tool execution plane.
   It exposes tools over MCP streamable HTTP, filters tools by permissions, and runs every tool through the `BaseTool.run()` orchestration wrapper.

3. `custom_code/` is the supported extension point.
   Operators can add custom MCP tools in `custom_code/tools/` and custom auth providers in `custom_code/auth_backends/` without editing the core packages.

4. Configuration is tenant-scoped and mostly stored in PostgreSQL.
   Tool config lives in `GSageToolConfig` (encrypted JSON), tool runtime state lives in `GSageToolState` (plain JSONB), and per-org auth chain/config lives in `GSageOrganization.auth_providers` plus `GSageOrganization.auth_config`.

5. Tool discovery is intentionally split.
   `list_tools` exposes only `core_tool=True` tools to keep MCP prompts small; the `search_tools` meta-tool is how the agent discovers the rest of the authorized catalog.

6. External authentication providers do not provision users manually.
   On successful external authentication, the backend syncs the local user, organization membership, role, groups, and optional departments.

---

## Current Runtime Map

| Area | Code | Runtime role |
|---|---|---|
| Backend API | `src/backend_api/app/` | FastAPI application, auth, JWT issuance, agent/session orchestration, approvals, background task APIs, org/user/admin operations |
| MCP server | `src/mcp_server/` | FastMCP server that lists and executes tools with permission filtering and audit logging |
| Shared auth | `src/shared/auth/` | Provider contract, provider registry, external user sync, built-in `local` and `ldap` providers |
| Shared models | `src/shared/models/` | SQLAlchemy models for org config, tool config/state, users, groups, departments, sessions, files, and audit projections |
| Web client | `web_client/` | React/Vite front-end for the main browser UI |
| Admin console | `admin_console/` | Textual/Rich terminal UI for operational and admin workflows |
| CLI client | `cli_client/` | Command-line client surface |
| Email worker | `src/email_worker/` | IMAP listener and email processing pipeline |
| Telegram worker | `src/telegram_worker/` | Telegram channel integration |
| Custom extensions | `custom_code/` | Supported location for operator-provided tools and auth backends |

---

## Current Docker Compose Topology

The active `docker-compose.yml` currently defines these runtime groups:

| Service group | Compose services | Purpose |
|---|---|---|
| Core application | `backend_api`, `mcp-server`, `frontend` | Main API, tool execution plane, and browser UI |
| Background execution | `celery-worker-default`, `celery-worker-tools`, `celery-worker-email`, `celery-worker-scheduled`, `celery-worker-knowledge`, `celery-worker-elasticsearch`, `celery-beat` | Async jobs, long-running tasks, scheduled maintenance, and indexing |
| Channel workers | `email-worker`, `telegram-worker` | Channel-specific ingestion and response loops |
| Knowledge and content services | `weaviate`, `wikijs`, `curator`, `minio`, `ollama` | Vector search, wiki integration, curator service, artifact storage, and local model/embedding services |
| Data stores and observability | `postgres`, `redis`, `elasticsearch`, `kibana` | Relational data, cache/queues, audit/metrics/logs, and observability UI |

This is materially different from older documentation that described a Flask UI or a smaller worker topology.

---

## Extension Points That Matter

| Purpose | Code path | Notes |
|---|---|---|
| Built-in MCP tools | `src/mcp_server/tools/` | Core, SOC, CRUD, and utility tools discovered at MCP startup |
| Custom MCP tools | `custom_code/tools/` | Auto-discovered if `CUSTOM_TOOLS_MODULE` points to `custom_code.tools` |
| Built-in auth providers | `src/shared/auth/backends/` | Today: `local` and `ldap` |
| Custom auth providers | `custom_code/auth_backends/` | Auto-discovered by `AuthProviderRegistry` |
| Tool config model | `src/shared/models/tool_config.py` | Encrypted org-scoped config with optional `profile_id` and `description` |
| Tool state model | `src/shared/models/tool_state.py` | Plain JSONB runtime state with reset policy |
| Org auth chain/config | `src/shared/models/organization.py` | `auth_providers` ordered JSON list plus encrypted `auth_config` |
| Tool/auth env defaults generator | `scripts/generate_env_defaults.py` | Regenerates the `.env.example` auto-generated zones |

---

## Tool Execution Flow

1. A client channel reaches `backend_api`.
2. The backend builds tenant headers and connects to `mcp-server` through MCP.
3. `mcp-server.list_tools` resolves tenant permissions and exposes only authorized core tools.
4. If the agent needs something else, it uses `search_tools` to discover non-core tools it is authorized to call.
5. `mcp-server.call_tool` resolves permissions again and delegates to `BaseTool.run()`.
6. `BaseTool.run()` handles permission checks, rate limiting, circuit breaker, config/state loading, retry logic, background dispatch, and audit logging.
7. Tool results are returned as canonical `ToolResult` payloads.

---

## Authentication Flow

1. The login route resolves the target organization.
2. The backend reads `GSageOrganization.auth_providers` to get the ordered provider chain.
3. `AuthProviderRegistry.authenticate_chain()` runs providers in order until one succeeds or a definitive rejection stops the chain.
4. For non-local providers, `upsert_external_user()` synchronizes the local user, org membership, role, groups, and optional departments.
5. The backend performs GuardianKey adaptive risk checks after credential validation.
6. The backend returns JWT tokens and, when applicable, the `must_change_password` flag.

---

## Recommended Reading Order

1. `TOOLS.md` if you want to build or extend MCP tools.
2. `AUTH_PROVIDERS.md` if you want to integrate external identity systems.
3. `src/mcp_server/tools/base.py` for the exact tool lifecycle contract.
4. `src/shared/auth/base.py` and `src/shared/auth/registry.py` for the exact auth provider contract.

---

## Documentation Rules For Contributors

- Keep these documents in English.
- Treat the code as the source of truth.
- Prefer documenting supported contracts and actual runtime behavior over aspirational architecture.
- When adding a tool or auth provider with configurable fields, also update the generated `.env.example` zones by running:

```bash
python scripts/generate_env_defaults.py
```