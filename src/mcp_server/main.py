"""gSage AI — MCP Server (streamable-http transport) entry point.

Uses the standard MCP protocol so that agno's ``MCPTools`` can connect
natively via ``streamable-http``.  Per-tenant identity is conveyed
through HTTP headers set by the ``header_provider`` lambda in
``agent_factory._build_mcp_tools``.

Headers expected on every HTTP request:
- ``X-Organization-ID`` — UUID of the tenant organisation
- ``X-User-ID``         — UUID of the requesting user
- ``X-Org-Role``        — role string (owner, admin, member, viewer, apikey)

The server resolves tool-level permissions from the database
(User → Groups → Permissions) and only exposes tools the user is
authorised to use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Optional

import anyio
import redis.asyncio as redis
import uvicorn
from mcp import types as mcp_types
from mcp.server.lowlevel import Server as MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.mcp_server.permissions import resolve_tool_permissions
from src.mcp_server.registry.registry import build_registry, get_registry, sync_permissions_to_db, sync_tools_to_db
from src.mcp_server.tenant_context import TenantHeaders, get_tenant_headers, get_tenant_headers_or_none
from src.mcp_server.tools.audit import ToolAuditLogger
from src.mcp_server.tools.base import _has_profile_permission
from src.shared.config.settings import get_settings
from src.shared.elasticsearch.client import ElasticsearchClient
from src.shared.logging import configure_logging
from src.shared.security.context import AgentContext, RequestSource

# Configure structured JSON logging before any other code runs
configure_logging("mcp", level="DEBUG" if get_settings().debug else "INFO")

logger = logging.getLogger(__name__)
settings = get_settings()


# ── App State ──────────────────────────────────────────────────────────────


class _AppState:
    redis_client: redis.Redis | None = None
    es_client: ElasticsearchClient | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None


_state = _AppState()


# ── Helper: build AgentContext from tenant headers ─────────────────────────


def _build_agent_context(
    tenant: TenantHeaders,
    tool_permissions: list[str],
) -> AgentContext:
    """Create an :class:`AgentContext` from resolved tenant headers."""
    # Map interface string to RequestSource enum (default to API)
    try:
        source = RequestSource(tenant.interface)
    except ValueError:
        source = RequestSource.API

    ctx = AgentContext(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        group_ids=[],  # not needed for tool filtering
        permissions=tool_permissions,
        request_id=uuid.uuid4(),
        source=source,
        dept_id=tenant.dept_id,
    )
    import logging as _logging
    _logging.getLogger(__name__).debug(
        "AgentContext built: org=%s user=%s dept_id=%s", ctx.org_id, ctx.user_id, ctx.dept_id
    )
    return ctx


def _base_tool_to_mcp(tool) -> mcp_types.Tool:
    """Convert a :class:`BaseTool` instance to an MCP ``Tool`` schema."""
    # Use effective_params_schema so tools that require approval have the
    # _approval_summary field automatically injected into their input schema.
    input_schema = tool.effective_params_schema

    # MCP standard annotations (destructiveHint) + custom metadata
    annotations = None
    meta = None
    if getattr(tool, "requires_approval", False):
        annotations = mcp_types.ToolAnnotations(destructiveHint=True)
        meta = {"requires_approval": True}

    if getattr(tool, "requires_user_credentials", False):
        meta = dict(meta or {})
        meta["requires_user_credentials"] = True
        cred_schema = getattr(tool, "credential_schema", None)
        if cred_schema:
            meta["credential_schema"] = cred_schema
        cred_ns = getattr(tool, "credential_namespace", None)
        if cred_ns:
            meta["credential_namespace"] = cred_ns

    return mcp_types.Tool(
        name=tool.name,
        description=getattr(tool, "__doc__", None) or f"{tool.name} (v{tool.version})",
        inputSchema=input_schema,
        annotations=annotations,
        _meta=meta,
    )


# ── MCP Protocol Server ───────────────────────────────────────────────────


mcp_server = MCPServer("gSage MCP Server")


@mcp_server.list_tools()
async def handle_list_tools() -> list[mcp_types.Tool]:
    """Return tools available on this MCP server.

    With tenant headers present (normal flow via ``server_params.headers``):
        Permission-filtered tool list — the LLM only sees what the user can use.

    Without tenant headers (fallback / health probes):
        Full catalogue — authorisation is always enforced at ``call_tool`` time.
    """
    registry = get_registry()
    tenant = get_tenant_headers_or_none()

    logger.info(
        "handle_list_tools ENTRY: tenant=%s session_factory=%s",
        f"org={tenant.org_id} user={tenant.user_id}" if tenant else "None",
        "yes" if _state.session_factory else "None",
    )

    # ── With tenant: permission-filtered list ──────────────────────────
    if tenant is not None and _state.session_factory is not None:
        try:
            tool_perms = await resolve_tool_permissions(
                tenant.org_id, tenant.user_id, _state.session_factory,
                interface=tenant.interface,
                dept_id=tenant.dept_id,
                redis_client=_state.redis_client,
            )
        except Exception as exc:
            logger.error(
                "list_tools: resolve_tool_permissions failed org=%s user=%s: %s",
                tenant.org_id, tenant.user_id, exc, exc_info=True,
            )
            tool_perms = []
        agent_ctx = _build_agent_context(tenant, tool_perms)
        tools = registry.get_tools(agent_ctx)

        # Only expose core tools in list_tools to reduce token usage.
        # Non-core tools remain callable and are discoverable via search_tools.
        core_tools = [t for t in tools if getattr(t, "core_tool", False)]

        logger.info(
            "list_tools: org=%s user=%s perms=%s → %d total / %d core tools: %s",
            tenant.org_id, tenant.user_id, tool_perms,
            len(tools), len(core_tools),
            ", ".join(sorted(t.name for t in core_tools)),
        )

        mcp_tools = [_base_tool_to_mcp(t) for t in core_tools]

        # ── Enrich tools via config-aware hook ────────────────────────────
        # Call enrich_for_listing() on every tool that has a config stored for
        # this org.  The hook returns an optional description suffix.  The
        # default implementation handles supports_multiple_configs (profiles);
        # subclasses override to expose hosts, presets, credentials, etc.
        if _state.session_factory is not None:
            async with _state.session_factory() as session:
                for i, tool in enumerate(core_tools):
                    try:
                        suffix = await tool.enrich_for_listing(tenant.org_id, session)
                    except Exception as exc:  # pragma: no cover
                        logger.warning(
                            "enrich_for_listing failed for tool=%s org=%s: %s",
                            tool.name, tenant.org_id, exc,
                        )
                        suffix = None

                    if not suffix:
                        continue

                    # For supports_multiple_configs, also inject enum + profile
                    # description into the config_profile schema field.
                    old = mcp_tools[i]
                    schema = dict(old.inputSchema)
                    if tool.supports_multiple_configs:
                        profiles = await tool.list_config_profiles(tenant.org_id, session)
                        visible = [
                            p for p in profiles
                            if _has_profile_permission(
                                tool_perms, tool.permissions, p["profile_id"]
                            )
                        ]
                        if visible:
                            enum_values = [p["profile_id"] for p in visible]
                            props = dict(schema.get("properties", {}))
                            if "config_profile" in props:
                                props["config_profile"] = {
                                    **props["config_profile"],
                                    "enum": enum_values,
                                }
                                schema["properties"] = props

                    existing_desc = old.description or ""
                    new_desc = (
                        f"{existing_desc}\n\n{suffix}"
                        if existing_desc
                        else suffix
                    )
                    mcp_tools[i] = mcp_types.Tool(
                        name=old.name,
                        description=new_desc,
                        inputSchema=schema,
                        annotations=old.annotations,
                        _meta=old.meta,
                    )

        return mcp_tools

    # ── Without tenant: full catalogue (fallback) ─────────────────────
    all_tools = registry.list_all()
    logger.info(
        "list_tools (no tenant): returning ALL %d tools as fallback",
        len(all_tools),
    )
    seen: set[str] = set()
    result: list[mcp_types.Tool] = []
    for t in all_tools:
        if t["name"] not in seen and t.get("is_latest", True):
            tool_obj = registry.get_tool(t["name"])
            if tool_obj:
                result.append(_base_tool_to_mcp(tool_obj))
                seen.add(t["name"])
    return result


@mcp_server.call_tool()
async def handle_call_tool(
    name: str,
    arguments: dict[str, Any] | None,
) -> list[mcp_types.TextContent]:
    """Execute a tool with full permission & rate-limit enforcement.

    Delegates to ``BaseTool.run()`` which handles:
    permission check, rate limiting, circuit breaker, config/state
    loading, retry, and audit logging.
    """
    tenant = get_tenant_headers()  # raises RuntimeError if missing

    if _state.session_factory is None or _state.redis_client is None or _state.es_client is None:
        return [mcp_types.TextContent(
            type="text",
            text=json.dumps({"status": "error", "error": {"code": "SERVICE_UNAVAILABLE", "message": "MCP Server not fully initialised"}}),
        )]

    # Resolve permissions early so we can build agent_ctx for audit logging
    try:
        tool_perms = await resolve_tool_permissions(
            tenant.org_id, tenant.user_id, _state.session_factory,
            interface=tenant.interface,
            dept_id=tenant.dept_id,
            redis_client=_state.redis_client,
        )
    except Exception as exc:
        logger.error(
            "call_tool: resolve_tool_permissions failed org=%s user=%s tool=%s: %s",
            tenant.org_id, tenant.user_id, name, exc, exc_info=True,
        )
        return [mcp_types.TextContent(
            type="text",
            text=json.dumps({"status": "error", "error": {"code": "PERMISSION_RESOLUTION_ERROR", "message": "Failed to resolve permissions"}}),
        )]
    agent_ctx = _build_agent_context(tenant, tool_perms)
    audit = ToolAuditLogger(_state.es_client)

    logger.debug(
        "call_tool: org=%s user=%s tool=%s args=%s",
        tenant.org_id, tenant.user_id, name, list((arguments or {}).keys()),
    )

    # ── Strip & inject per-tool user credential (passed inline by the agent
    # proxy as a reserved ``_user_credential`` arg, never set by the LLM) ──
    injected_credential: Optional[dict] = None
    if arguments and "_user_credential" in arguments:
        cred = arguments.pop("_user_credential")
        if isinstance(cred, dict):
            injected_credential = cred

    registry = get_registry()
    tool = registry.get_tool(name)
    if tool is None:
        logger.warning("call_tool: TOOL_NOT_FOUND '%s' (org=%s)", name, tenant.org_id)
        await audit.log_execution(
            agent_ctx, name, "0.0.0",
            arguments or {}, "error", 0, "TOOL_NOT_FOUND",
        )
        return [mcp_types.TextContent(
            type="text",
            text=json.dumps({"status": "error", "error": {"code": "TOOL_NOT_FOUND", "message": f"Tool '{name}' not found"}}),
        )]

    # Key the credential under the tool's namespace so tools that share a
    # ``credential_namespace`` see the same credential dict.
    if injected_credential is not None:
        cred_key = getattr(tool, "credential_namespace", None) or tool.name
        agent_ctx.user_credentials[cred_key] = injected_credential
        logger.debug(
            "call_tool: injected user credential for tool=%s under key=%s (fields=%s)",
            name, cred_key,
            sorted(k for k, v in injected_credential.items() if v is not None),
        )

    async with _state.session_factory() as session:
        result = await tool.run(
            agent_context=agent_ctx,
            params=arguments or {},
            session=session,
            redis_client=_state.redis_client,
            es_client=_state.es_client,
            gsage_session_id=tenant.gsage_session_id,
        )

    return [mcp_types.TextContent(
        type="text",
        text=json.dumps(result.to_dict()),
    )]


# ── Session Manager ───────────────────────────────────────────────────────

_session_manager = StreamableHTTPSessionManager(
    app=mcp_server,
    stateless=True,  # each request is independent; headers vary per tenant
)


# ── ASGI middleware (raw) for tenant header extraction ─────────────────────


class _TenantHeaderASGI:
    """Thin ASGI wrapper that extracts tenant headers into a contextvar."""

    def __init__(self, app: Callable[[Any, Any, Any], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from src.mcp_server.tenant_context import _tenant_var

            raw_headers = dict(scope.get("headers", []))

            # Diagnostic: dump all received HTTP header names
            header_names = [k.decode(errors="replace") for k in raw_headers.keys()]
            logger.info(
                "ASGI headers received: %s",
                ", ".join(sorted(header_names)),
            )

            org_id_raw = raw_headers.get(b"x-organization-id", b"").decode()
            user_id_raw = raw_headers.get(b"x-user-id", b"").decode()
            org_role = raw_headers.get(b"x-org-role", b"member").decode()
            session_id_raw = raw_headers.get(b"x-gsage-session-id", b"").decode()

            logger.info(
                "Tenant header extraction: org_id=%r user_id=%r role=%r session_id=%r",
                org_id_raw or None, user_id_raw or None, org_role, session_id_raw or None,
            )

            if org_id_raw and user_id_raw:
                try:
                    gsage_session_id = uuid.UUID(session_id_raw) if session_id_raw else None
                    dept_id_raw = raw_headers.get(b"x-department-id", b"").decode()
                    dept_id: uuid.UUID | None = None
                    if dept_id_raw:
                        try:
                            dept_id = uuid.UUID(dept_id_raw)
                        except ValueError:
                            pass  # Malformed — ignore silently
                    tenant = TenantHeaders(
                        org_id=uuid.UUID(org_id_raw),
                        user_id=uuid.UUID(user_id_raw),
                        org_role=org_role,
                        gsage_session_id=gsage_session_id,
                        dept_id=dept_id,
                    )
                    _tenant_var.set(tenant)
                    logger.info("Tenant contextvar SET: org=%s user=%s session=%s dept_id=%s", tenant.org_id, tenant.user_id, tenant.gsage_session_id, tenant.dept_id)
                except ValueError as ve:
                    logger.warning("Invalid tenant header UUIDs: %s", ve)
            else:
                logger.info("Tenant headers missing — contextvar NOT set")

        await self.app(scope, receive, send)


# ── ASGI handler for the /mcp endpoint ─────────────────────────────────────


class _MCPEndpoint:
    """ASGI app that delegates to the session manager + tenant middleware."""

    def __init__(self):
        self._inner = _TenantHeaderASGI(self._handle)

    async def _handle(self, scope, receive, send):
        await _session_manager.handle_request(scope, receive, send)

    async def __call__(self, scope, receive, send):
        await self._inner(scope, receive, send)


# ── Starlette App (health + MCP) ──────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: Starlette):
    """Start-up: connect dependencies. Shutdown: close cleanly."""
    # Redis
    _state.redis_client = redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )

    # Elasticsearch
    _state.es_client = ElasticsearchClient()
    es_ok = await _state.es_client.health_check()
    if es_ok:
        logger.info("Elasticsearch connected — creating index templates")
        await _state.es_client.create_index_templates({})
    else:
        logger.warning("Elasticsearch not reachable — audit logs will fail")

    # PostgreSQL async engine
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    _state.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Build tool registry once at start-up
    build_registry()
    await sync_permissions_to_db(get_registry(), _state.session_factory)
    await sync_tools_to_db(get_registry(), _state.session_factory)
    all_tools = get_registry().list_all()
    logger.info("MCP Server ready — %d tools registered", len(all_tools))
    logger.debug(
        "MCP Server config: url=%s, redis=%s, es=%s, db=%s",
        settings.mcp_server_url,
        settings.redis_url.split("@")[-1] if settings.redis_url else "N/A",
        settings.elasticsearch_url,
        f"{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}",
    )
    logger.debug(
        "Registered tools: %s",
        ", ".join(sorted(t["name"] for t in all_tools)),
    )

    # Give the session manager a task group for its background tasks
    async with anyio.create_task_group() as tg:
        _session_manager._task_group = tg
        yield

    # Shutdown
    if _state.redis_client:
        await _state.redis_client.aclose()
    if _state.es_client:
        await _state.es_client.close()
    await engine.dispose()


async def _health_endpoint(request: Request) -> JSONResponse:
    """Health check — returns 200 when the server and Redis are up."""
    redis_ok = False
    if _state.redis_client:
        try:
            await _state.redis_client.ping()
            redis_ok = True
        except Exception:
            pass
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "mcp-server",
            "redis": "ok" if redis_ok else "unavailable",
        },
    )


async def _tools_debug_endpoint(request: Request) -> JSONResponse:
    """Debug endpoint — list all registered tools (no auth required)."""
    registry = get_registry()
    all_tools = registry.list_all()
    return JSONResponse(
        status_code=200,
        content={
            "total": len(all_tools),
            "tools": [
                {
                    "name": t["name"],
                    "version": t.get("version", "?"),
                    "tag": t.get("tag", "?"),
                }
                for t in all_tools
            ],
        },
    )


async def _catalog_endpoint(request: Request) -> JSONResponse:
    """Compact non-core tool catalog for system prompt injection.

    Returns permission-filtered non-core tools grouped by category so that
    the backend can inject a concise discoverable-tools catalog into the
    agent system prompt.  The tenant is identified by the same headers used
    by the MCP protocol (X-Organization-ID, X-User-ID, X-Org-Role).
    """
    from collections import defaultdict

    # Extract tenant headers directly from the Starlette request (this
    # endpoint is served outside the _TenantHeaderASGI wrapper).
    org_id_raw = request.headers.get("x-organization-id", "")
    user_id_raw = request.headers.get("x-user-id", "")
    org_role = request.headers.get("x-org-role", "member")
    interface = request.headers.get("x-interface", "web")
    session_id_raw = request.headers.get("x-gsage-session-id", "")
    dept_id_raw = request.headers.get("x-department-id", "")

    registry = get_registry()

    if org_id_raw and user_id_raw and _state.session_factory is not None:
        try:
            gsage_session_id = uuid.UUID(session_id_raw) if session_id_raw else None
            dept_id: uuid.UUID | None = None
            if dept_id_raw:
                try:
                    dept_id = uuid.UUID(dept_id_raw)
                except ValueError:
                    pass

            tenant = TenantHeaders(
                org_id=uuid.UUID(org_id_raw),
                user_id=uuid.UUID(user_id_raw),
                org_role=org_role,
                interface=interface,
                gsage_session_id=gsage_session_id,
                dept_id=dept_id,
            )
            tool_perms = await resolve_tool_permissions(
                tenant.org_id, tenant.user_id, _state.session_factory,
                interface=tenant.interface,
                dept_id=tenant.dept_id,
                redis_client=_state.redis_client,
            )
            agent_ctx = _build_agent_context(tenant, tool_perms)
            tools = registry.get_tools(agent_ctx)
        except (ValueError, Exception) as exc:
            logger.warning("catalog_endpoint: failed to resolve tenant tools: %s", exc)
            tools = []
    else:
        # No tenant headers — return empty catalog (auth enforced at call time).
        tools = []

    # Only non-core tools belong in the discoverable catalog.
    non_core = [t for t in tools if not getattr(t, "core_tool", False)]

    # Group by category: {category: [name, ...]}
    by_category: dict[str, list[str]] = defaultdict(list)
    for t in non_core:
        cat = getattr(t, "category", "general") or "general"
        by_category[cat].append(t.name)

    return JSONResponse(
        status_code=200,
        content={"categories": {k: sorted(v) for k, v in sorted(by_category.items())}},
    )


app = Starlette(
    routes=[
        Route("/health", _health_endpoint, methods=["GET"]),
        Route("/tools", _tools_debug_endpoint, methods=["GET"]),
        Route("/tools/catalog", _catalog_endpoint, methods=["GET"]),
        Route("/", _MCPEndpoint(), methods=["GET", "POST", "DELETE"]),
    ],
    lifespan=_lifespan,
)


# ── Entry point ────────────────────────────────────────────────────────────


if __name__ == "__main__":
    uvicorn.run(
        "src.mcp_server.main:app",
        host="0.0.0.0",
        port=8001,
        log_level="info",
    )
