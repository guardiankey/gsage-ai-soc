# Building MCP Tools For gSage AI

This document explains how tools work today in gSage AI and how to add a new
tool that integrates cleanly with the MCP server, permission model, audit
pipeline, and per-organization configuration system.

The source of truth for this document is:

- `src/mcp_server/tools/base.py`
- `src/mcp_server/registry/registry.py`
- `src/mcp_server/main.py`
- `src/shared/models/tool_config.py`
- `src/shared/models/tool_state.py`

---

## Fast Path

If you only need the shortest possible path, do this:

1. Copy `custom_code/tools/example_tool.py` into a new module under `custom_code/tools/`.
2. Create a concrete `BaseTool` subclass with a unique `name` and an `execute()` implementation.
3. Define `permissions`, `summary`, `category`, and `params_schema`.
4. If the tool needs per-org config, add `config_defaults`, `config_schema`, and optionally `requires_config = True`.
5. Restart the MCP server.
6. Grant the tool permission tags to a group and configure the organization's tool profile if needed.

There is no manual registration step.

---

## Core Concepts In Two Minutes

1. A tool is a concrete `BaseTool` subclass.
   You implement `execute()`. The framework handles almost everything around it.

2. Discovery is automatic.
   The registry scans built-in tool packages and the configured custom tool package (`CUSTOM_TOOLS_MODULE`, default `custom_code.tools`).

3. The agent never sees the full catalog by default.
   `list_tools` exposes only `core_tool=True` tools that the current user is allowed to use. The `search_tools` meta-tool is the discovery surface for the rest.

4. Permissions are enforced twice.
   Tools are hidden from the LLM when the user lacks the required permission, and `call_tool` checks permissions again before execution.

5. Config and runtime state are organization-scoped.
   Tool config is encrypted in PostgreSQL (`GSageToolConfig`). Tool state is stored as plain JSONB (`GSageToolState`).

6. `BaseTool.run()` is the real execution pipeline.
   It handles permission checks, rate limiting, circuit breaker, config loading, state loading, retries, background dispatch, state persistence, and audit logging.

---

## Where Tools Live

| Location | What belongs there |
|---|---|
| `src/mcp_server/tools/core/` | Core utility tools and meta-tools always close to the agent workflow |
| `src/mcp_server/tools/soc/` | SOC-oriented tools grouped by domain such as network, email, threat intel, response, EDR, monitoring, ticketing, and admin |
| `src/mcp_server/tools/crud/` | Direct database CRUD tools, gated by feature flags |
| `custom_code/tools/` | Operator-provided tools that should survive core upgrades |

Subdirectories are supported. Keep `__init__.py` files in any subpackage you want the registry to recurse into.

Examples from the current codebase:

- `src/mcp_server/tools/core/search_tools.py`
- `src/mcp_server/tools/core/mermaid_validate.py`
- `src/mcp_server/tools/soc/network/dns_lookup.py`
- `src/mcp_server/tools/soc/response/block_ip.py`
- `src/mcp_server/tools/soc/ticket/glpi/glpi_create_ticket.py`
- `custom_code/tools/example_tool.py`

---

## Discovery, Registration, And Visibility

### Auto-discovery rules

At MCP startup, `build_registry()` walks the configured packages and registers any class that is:

1. a concrete subclass of `BaseTool`
2. not abstract
3. has a `name`
4. has `available = True` or does not define `available`

Built-in infrastructure modules such as `base`, `audit`, `circuit_breaker`, and `crud_base` are skipped.

### What startup syncs to the database

At MCP startup the server also:

- syncs tool metadata into `gsage_tools`
- syncs declared permission tags into `gsage_permissions`

That means a new tool becomes visible to admin/config surfaces after startup without manual SQL changes.

### What the LLM actually sees

The current visibility model is intentionally split:

- `list_tools` returns only tools where `core_tool = True` and the user has permission.
- `search_tools` is itself a core tool with no required permission and returns the rest of the user-visible catalog.
- `call_tool` still re-validates permission before running anything.

If your tool should be discoverable but not always injected into the MCP tool list, leave `core_tool = False` and make sure `summary` and `category` are useful so `search_tools` can surface it.

---

## Minimal Tool Skeleton

```python
from __future__ import annotations

from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext


class MyTool(BaseTool):
    """Short description used in MCP and admin metadata."""

    name: ClassVar[str] = "my_tool"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "One-line summary used by search_tools"
    category: ClassVar[str] = "utility"
    permissions: ClassVar[list[str]] = ["utility:run"]

    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 15
    use_circuit_breaker: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
                "description": "Input value to process",
            }
        },
        "required": ["value"],
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "properties": {
            "prefix": {
                "type": "string",
                "description": "Optional prefix used in the output",
            }
        },
        "required": [],
    }
    config_defaults: ClassVar[dict] = {"prefix": ""}

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        raw_value = params.get("value")
        if not isinstance(raw_value, str) or not raw_value.strip():
            return self._failure("INVALID_INPUT", "'value' must be a non-empty string")

        result = {
            "value": f"{config.get('prefix', '')}{raw_value.strip()}",
            "org_id": str(agent_context.org_id),
        }
        return self._success(result)
```

Use the `_success()`, `_failure()`, and `_partial()` helpers unless you have a strong reason to build `ToolResult` manually.

---

## Recommended Class Variables

### Discovery and agent UX

| Attribute | Required | Meaning |
|---|---|---|
| `name` | yes | Unique tool identifier |
| `version` | no | Semantic version string, default `1.0.0` |
| `summary` | strongly recommended | One-line summary used by `search_tools` and DB metadata |
| `category` | strongly recommended | Search category; use one of the established categories when possible |
| `core_tool` | no | If `True`, the tool can be exposed by `list_tools` |
| `available` | no | If `False`, the tool is skipped by the registry |

Recommended categories from the current `search_tools` UX are:

- `dns`
- `network`
- `email`
- `threat_intel`
- `file`
- `document`
- `itsm`
- `edr`
- `kb`
- `crud`
- `firewall`
- `security`
- `utility`

### Safety and runtime behavior

| Attribute | Default | Meaning |
|---|---|---|
| `permissions` | `[]` | Permission tags required to use the tool |
| `rate_limit_per_minute` | `60` | Per-org, per-tool, per-profile Redis rate limit |
| `timeout_seconds` | `30` | Timeout applied around `execute()` |
| `use_circuit_breaker` | `True` | Use Redis-backed circuit breaker for retryable failures |
| `requires_approval` | `False` | Marks the tool as approval-sensitive for the agent layer |
| `always_background` | `False` | Always dispatch the tool to the background worker |
| `background_threshold_seconds` | `None` | On timeout, fall back to background execution instead of failing |
| `supports_multiple_configs` | `False` | Enables multiple config profiles per org for the same tool |
| `audit_field_mapping` | `{}` | Auto-map params into audit context without relying on the LLM |
| `audit_output` | `True` | Whether to store `ToolResult.data` in audit output |

### Config and state

| Attribute | Default | Meaning |
|---|---|---|
| `params_schema` | `None` | JSON Schema-like input contract for the tool call |
| `config_schema` | `None` | Schema for org-scoped configuration |
| `config_defaults` | `{}` | Lowest-priority config layer |
| `requires_config` | `False` | Fail if there is no usable config at all |
| `state_schema` | `None` | Schema for stored runtime state |
| `state_defaults` | `{}` | Default runtime state |
| `reset_policy` | `"never"` | One of `daily`, `monthly`, or `never` |

For new tools, prefer a JSON Schema-style shape with `properties`, `required`, and `additionalProperties`. The code still tolerates older flat field maps for config introspection, but the JSON Schema style matches the rest of the platform better.

---

## What `BaseTool.run()` Does For You

Do not duplicate these concerns inside `execute()` unless you have a very specific reason.

`BaseTool.run()` currently handles:

1. framework-injected params (`config_profile`, `_audit_context`)
2. profile-aware permission checks
3. Redis rate limiting
4. circuit breaker checks
5. config loading and merge
6. state loading
7. optional background pre-flight dispatch
8. required-parameter presence validation from `params_schema`
9. timeout handling and retryable retries
10. circuit breaker feedback
11. state persistence
12. audit logging to Elasticsearch

The retry model is currently:

- maximum 2 automatic retries after the first attempt
- backoff intervals of 1 second and 2 seconds
- retries only when the result is an error marked `retryable=True`, or when `execute()` raises an exception / times out and the wrapper creates a retryable failure

---

## Parameters, Validation, And Framework-Injected Fields

### `params_schema`

`params_schema` is what the agent sees as the tool input contract. Required fields are checked before `execute()` runs, but type validation is still your responsibility.

Treat all params as untrusted agent input.

```python
raw = params.get("ip")
if not isinstance(raw, str) or not raw.strip():
    return self._failure("INVALID_INPUT", "'ip' must be a non-empty string")
```

### Fields the framework injects

Depending on tool features, the framework may inject these fields into the visible input schema:

| Field | When it appears | What happens at runtime |
|---|---|---|
| `config_profile` | `supports_multiple_configs=True` | Stripped before `execute()` and used to select the config/state profile |
| `_approval_summary` | `requires_approval=True` | Exposed to the agent so a human-readable approval summary can be collected; today it is not stripped automatically, so your tool should tolerate it if direct execution still occurs |
| `_audit_context` | all tools | Stripped before `execute()` and stored only for audit |

If you do not need `_approval_summary`, simply ignore it in `params`.

---

## Permission Model

Declare base permission tags as normal two-segment strings such as:

- `dns:read`
- `email:send`
- `ticket:write`

For multi-profile tools, the platform already understands these granted forms at runtime:

- exact base tag: `email:send`
- all-profiles wildcard: `email:send:*`
- profile-specific grant: `email:send:prod_smtp`
- glob patterns via `fnmatch`, such as `email:*`

That means the tool class should usually declare only the base permission, while admins can refine access by profile in the database.

---

## Config Layers And Environment Defaults

The effective tool config is merged in this order:

1. `config_defaults` from the class
2. `TOOL_<TOOL_NAME>__<FIELD>` environment variables
3. encrypted per-org config row from `GSageToolConfig`

That means the highest-priority value is the per-organization DB config.

### Environment variable naming

For a tool named `my_tool`, the prefix is:

```env
TOOL_MY_TOOL__FIELD_NAME=value
```

### YAML defaults next to the module

You can also provide a same-stem YAML file next to the Python module:

```text
custom_code/tools/my_tool.py
custom_code/tools/my_tool.yaml
```

YAML values are merged into `config_defaults`, but class-defined `config_defaults` win on collisions.

### Updating `.env.example`

After adding or changing configurable tools, regenerate the auto-generated environment defaults zone:

```bash
python scripts/generate_env_defaults.py
```

---

## Config Profiles And Listing Enrichment

If `supports_multiple_configs = True`, the platform supports multiple config rows for the same tool within the same organization.

Each profile is stored as a `GSageToolConfig` row with:

- `tool_name`
- `profile_id`
- optional `description`
- encrypted `config`

The default `enrich_for_listing()` implementation adds a profile summary to the tool description in `list_tools`, and `mcp_server.main.handle_list_tools()` injects the visible `config_profile` enum into the MCP schema.

You can override `enrich_for_listing()` if your tool needs to expose richer runtime hints, such as hosts, presets, or environment labels.

---

## Tool State

Tool state is persisted in `GSageToolState` as plain JSONB and is scoped by organization and profile.

Use cases include:

- quota tracking
- last sync timestamps
- incremental cursors
- rate or usage counters

Helpers available in `BaseTool`:

- `load_state()`
- `save_state()`
- `update_state_atomic()` for JSONB field updates without read-modify-write races

State is not encrypted. Use it for operational data, not secrets.

---

## Background Execution And Approval-Aware Tools

### Background execution

There are three ways a tool can end up in the background queue:

1. `always_background = True`
2. overriding `should_run_background()` to decide based on params/config
3. setting `background_threshold_seconds` so a synchronous timeout falls back to a background task instead of an immediate error

When this happens, the tool returns a `ToolResult` with `status = "background"` and a `task_id`.

### Approval-aware tools

If `requires_approval = True`, the MCP server advertises that fact through MCP annotations and `_meta`. The approval gate itself lives in the agent layer, not inside the MCP server.

In other words:

- the tool is marked as destructive / approval-sensitive
- the agent is expected to collect approval before calling it
- the tool implementation should still be safe if called directly

---

## Files And Artifacts

`BaseTool` includes file helpers for tools that need to read or emit files.

### `_store_file()`

Uploads bytes to MinIO and records the file in the database. On success it returns a dict containing fields such as:

- `file_id`
- `filename`
- `content_type`
- `size_bytes`

This is the correct path for generated artifacts such as reports, rendered images, archives, or exported documents.

### `_load_file()`

Loads a previously stored file by `file_id`, validates org/user ownership rules, and returns metadata plus bytes.

This is the correct path for tools that need to process files already uploaded into the platform.

---

## CRUD Tools And Feature Flags

The project also has a special `CrudBaseTool` for direct database CRUD operations.

Important runtime flags:

- `CRUD_TOOLS_ENABLED=true` enables built-in CRUD tools and runtime access
- `CRUD_TOOLS_ALLOW_WRITE=true` additionally enables CRUD write actions
- `CUSTOM_TOOLS_MODULE=""` disables loading custom tools entirely

If you are building a normal integration against an external system, subclass `BaseTool`, not `CrudBaseTool`.

---

## Common Pitfalls

1. Do not manually register a tool.
   Discovery is automatic.

2. Do not duplicate permission, retry, or audit logic inside `execute()`.
   That belongs to `BaseTool.run()`.

3. Do not assume type safety from the LLM.
   Validate every parameter explicitly.

4. Do not mark too many tools as `core_tool=True`.
   `list_tools` is intentionally small.

5. Do not store secrets in state.
   Use encrypted config.

6. Do not forget permission assignment.
   A correctly implemented tool will still be invisible until a group has the right tag.

---

## Rollout Checklist

1. Add the tool module under `custom_code/tools/`.
2. Add `__init__.py` files for any new subpackages.
3. Define `name`, `permissions`, `summary`, `category`, and `params_schema`.
4. Add config/state declarations if needed.
5. Optionally add same-stem YAML defaults.
6. Run `python scripts/generate_env_defaults.py` if you added config fields.
7. Restart `mcp-server`.
8. Configure the tool for the target organization if it needs config.
9. Assign the permission tags to a group.
10. Verify discovery through `search_tools` or, if appropriate, `list_tools`.