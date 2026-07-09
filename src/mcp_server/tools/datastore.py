"""gSage AI — DataStore MCP Tool.

Provides the LLM with named dynamic data stores backed by PostgreSQL JSONB.
Uses its own DATASTORE_ENABLED feature flag (independent from CRUD_TOOLS_ENABLED).

Supported actions:
    list_stores, describe_store, create_store, update_store, delete_store,
    query, insert, bulk_insert, update_record, delete_record
"""

from __future__ import annotations

import contextvars
import time
import uuid
from typing import ClassVar, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

# Task-local storage for the DB session (async-task–scoped, concurrency-safe).
_datastore_session_var: contextvars.ContextVar[Optional[AsyncSession]] = contextvars.ContextVar(
    "_datastore_session_var", default=None
)


class DataStoreTool(BaseTool):
    """LLM-facing tool for dynamic, dept-scoped data stores."""

    name: ClassVar[str] = "datastore"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Persistent key-value data store for saving and retrieving structured data across conversations"
    category: ClassVar[str] = "utility"
    permissions: ClassVar[list[str]] = ["datastores:read"]
    use_circuit_breaker: ClassVar[bool] = False  # local DB — no external dependency

    valid_actions: ClassVar[frozenset[str]] = frozenset(
        {
            "list_stores",
            "describe_store",
            "create_store",
            "update_store",
            "delete_store",
            "query",
            "insert",
            "bulk_insert",
            "update_record",
            "delete_record",
        }
    )
    write_actions: ClassVar[frozenset[str]] = frozenset(
        {
            "create_store",
            "update_store",
            "delete_store",
            "insert",
            "bulk_insert",
            "update_record",
            "delete_record",
        }
    )
    write_permission: ClassVar[str] = "datastores:write"

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list_stores",
                    "describe_store",
                    "create_store",
                    "update_store",
                    "delete_store",
                    "query",
                    "insert",
                    "bulk_insert",
                    "update_record",
                    "delete_record",
                ],
                "description": "Action to perform on the data store.",
            },
            "store_id": {
                "type": "string",
                "description": "UUID of the target store (for most record/store actions).",
            },
            "store_name": {
                "type": "string",
                "description": "Name of the store — alternative to store_id for describe_store.",
            },
            "name": {
                "type": "string",
                "description": "Store name (for create_store or update_store).",
            },
            "description": {
                "type": "string",
                "description": "Human-readable description of the store (LLM context).",
            },
            "schema": {
                "type": "object",
                "description": "JSON Schema draft-07 for record validation. Pass {} to disable validation.",
            },
            "visibility": {
                "type": "string",
                "enum": ["private", "shared"],
                "description": "Visibility: 'private' (owner-only) or 'shared' (all department members).",
            },
            "max_records": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum number of records allowed in the store.",
            },
            "is_active": {
                "type": "boolean",
                "description": "Set False to soft-disable a store (update_store only).",
            },
            "data": {
                "type": "object",
                "description": "Record payload for insert or update_record.",
            },
            "records": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Array of record payloads for bulk_insert.",
            },
            "record_id": {
                "type": "string",
                "description": "UUID of the target record (for update_record / delete_record).",
            },
            "filters": {
                "type": "object",
                "description": "JSONB containment filter for query — records must contain these key/values.",
            },
            "page": {"type": "integer", "minimum": 1, "description": "1-based page number."},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Results per page."},
        },
        "required": ["action"],
    }

    # ── Session access ───────────────────────────────────────────────────────

    def _get_session(self) -> Optional[AsyncSession]:
        return _datastore_session_var.get()

    # ── Feature-flag + session injection (override run()) ────────────────────

    async def run(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        redis_client,
        es_client,
        gsage_session_id=None,
        tool_call_id=None,
    ) -> ToolResult:
        """Check DATASTORE_ENABLED, set session ContextVar, then delegate to BaseTool.run()."""
        from src.shared.config.settings import get_settings

        if not get_settings().datastore_enabled:
            return self._failure(
                code="FEATURE_DISABLED",
                message="DataStore is disabled. Set DATASTORE_ENABLED=true to enable.",
            )

        token = _datastore_session_var.set(session)
        try:
            return await super().run(agent_context, params, session, redis_client, es_client, gsage_session_id=gsage_session_id)
        finally:
            _datastore_session_var.reset(token)

    # ── BaseTool.execute — main dispatcher ───────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        start = time.monotonic()

        action = params.get("action")
        if not action or action not in self.valid_actions:
            return self._failure(
                code="INVALID_ACTION",
                message=f"Invalid action. Valid: {', '.join(sorted(self.valid_actions))}",
            )

        # Write-action permission check
        if action in self.write_actions:
            if "*" not in agent_context.permissions and self.write_permission not in agent_context.permissions:
                return self._failure(
                    code="PERMISSION_DENIED",
                    message=f"Action '{action}' requires permission: {self.write_permission}",
                )

        session = self._get_session()
        if session is None:
            return self._failure(
                code="INTERNAL_ERROR",
                message="DB session not available.",
            )

        handler = getattr(self, f"_handle_{action}", None)
        if handler is None:
            return self._failure(code="NOT_IMPLEMENTED", message=f"Handler for '{action}' not implemented.")

        try:
            return await handler(agent_context, params, session, start)
        except Exception as exc:
            from src.shared.services.datastore_service import DataStoreError

            if isinstance(exc, DataStoreError):
                return self._failure(code="DATASTORE_ERROR", message=str(exc))
            raise

    # ── Action handlers ──────────────────────────────────────────────────────

    async def _handle_list_stores(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        from src.shared.services import datastore_service

        page = max(1, int(params.get("page", 1)))
        page_size = min(200, max(1, int(params.get("page_size", 50))))

        if agent_context.dept_id is None:
            return self._failure(
                code="MISSING_CONTEXT",
                message="No active department in session context. Set a department before listing stores.",
            )

        stores, total = await datastore_service.list_stores(
            session=session,
            org_id=agent_context.org_id,
            user_id=agent_context.user_id,
            dept_id=agent_context.dept_id,
            page=page,
            page_size=page_size,
        )
        ms = int((time.monotonic() - start) * 1000)
        return self._success(
            data={
                "stores": [_store_to_dict(s) for s in stores],
                "total": total,
                "page": page,
                "page_size": page_size,
            },
            execution_time_ms=ms,
        )

    async def _handle_describe_store(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        from src.shared.services import datastore_service

        store_id = params.get("store_id")
        store_name = params.get("store_name")

        if agent_context.dept_id is None:
            return self._failure(
                code="MISSING_CONTEXT",
                message="No active department in session context. Set a department before describing stores.",
            )

        if store_id:
            store = await datastore_service.get_store(
                session, agent_context.org_id, agent_context.user_id, uuid.UUID(store_id),
                dept_id=agent_context.dept_id,
            )
        elif store_name:
            store = await datastore_service.get_store_by_name(
                session, agent_context.org_id, agent_context.user_id, store_name,
                dept_id=agent_context.dept_id,
            )
        else:
            return self._failure(code="MISSING_PARAM", message="Provide 'store_id' or 'store_name'.")

        ms = int((time.monotonic() - start) * 1000)
        return self._success(data=_store_to_dict(store), execution_time_ms=ms)

    async def _handle_create_store(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        from src.shared.services import datastore_service

        name = params.get("name")
        if not name:
            return self._failure(code="MISSING_PARAM", message="'name' is required for create_store.")

        if agent_context.dept_id is None:
            return self._failure(
                code="MISSING_CONTEXT",
                message="No active department in session context. The user must select a department (via the 'dept set' command or web UI) before creating stores. Ask the user to set a department first.",
            )

        store = await datastore_service.create_store(
            session=session,
            org_id=agent_context.org_id,
            user_id=agent_context.user_id,
            dept_id=agent_context.dept_id,
            name=name,
            description=params.get("description"),
            schema=params.get("schema"),
            visibility=params.get("visibility", "shared"),
            max_records=int(params.get("max_records", 500)),
        )
        ms = int((time.monotonic() - start) * 1000)
        return self._success(data=_store_to_dict(store), execution_time_ms=ms)

    async def _handle_update_store(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        from src.shared.services import datastore_service

        store_id = params.get("store_id")
        if not store_id:
            return self._failure(code="MISSING_PARAM", message="'store_id' is required for update_store.")

        if agent_context.dept_id is None:
            return self._failure(
                code="MISSING_CONTEXT",
                message="No active department in session context. Set a department before updating stores.",
            )

        store = await datastore_service.update_store(
            session=session,
            org_id=agent_context.org_id,
            user_id=agent_context.user_id,
            store_id=uuid.UUID(store_id),
            dept_id=agent_context.dept_id,
            name=params.get("name"),
            description=params.get("description"),
            schema=params.get("schema"),
            visibility=params.get("visibility"),
            max_records=int(params["max_records"]) if "max_records" in params else None,
            is_active=params.get("is_active"),
        )
        ms = int((time.monotonic() - start) * 1000)
        return self._success(data=_store_to_dict(store), execution_time_ms=ms)

    async def _handle_delete_store(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        from src.shared.services import datastore_service

        store_id = params.get("store_id")
        if not store_id:
            return self._failure(code="MISSING_PARAM", message="'store_id' is required for delete_store.")

        if agent_context.dept_id is None:
            return self._failure(
                code="MISSING_CONTEXT",
                message="No active department in session context. Set a department before deleting stores.",
            )

        await datastore_service.delete_store(
            session, agent_context.org_id, agent_context.user_id, uuid.UUID(store_id),
            dept_id=agent_context.dept_id,
        )
        ms = int((time.monotonic() - start) * 1000)
        return self._success(data={"deleted": True, "store_id": store_id}, execution_time_ms=ms)

    async def _handle_query(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        from src.shared.services import datastore_service

        store_id = params.get("store_id")
        if not store_id:
            return self._failure(code="MISSING_PARAM", message="'store_id' is required for query.")

        if agent_context.dept_id is None:
            return self._failure(
                code="MISSING_CONTEXT",
                message="No active department in session context. Set a department before querying stores.",
            )

        # Ensure user can see the store before accessing records
        await datastore_service.get_store(
            session, agent_context.org_id, agent_context.user_id, uuid.UUID(store_id),
            dept_id=agent_context.dept_id,
        )

        page = max(1, int(params.get("page", 1)))
        page_size = min(200, max(1, int(params.get("page_size", 50))))

        records, total = await datastore_service.query_records(
            session=session,
            store_id=uuid.UUID(store_id),
            filters=params.get("filters"),
            page=page,
            page_size=page_size,
        )
        ms = int((time.monotonic() - start) * 1000)
        return self._success(
            data={
                "records": [_record_to_dict(r) for r in records],
                "total": total,
                "page": page,
                "page_size": page_size,
            },
            execution_time_ms=ms,
        )

    async def _handle_insert(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        from src.shared.services import datastore_service

        store_id = params.get("store_id")
        data = params.get("data")
        if not store_id:
            return self._failure(code="MISSING_PARAM", message="'store_id' is required.")
        if data is None:
            return self._failure(code="MISSING_PARAM", message="'data' is required.")

        if agent_context.dept_id is None:
            return self._failure(
                code="MISSING_CONTEXT",
                message="No active department in session context. Set a department before inserting records.",
            )

        store = await datastore_service.get_store(
            session, agent_context.org_id, agent_context.user_id, uuid.UUID(store_id),
            dept_id=agent_context.dept_id,
        )
        record = await datastore_service.insert_record(session, store, data)
        ms = int((time.monotonic() - start) * 1000)
        return self._success(data=_record_to_dict(record), execution_time_ms=ms)

    async def _handle_bulk_insert(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        from src.shared.services import datastore_service

        store_id = params.get("store_id")
        records = params.get("records")
        if not store_id:
            return self._failure(code="MISSING_PARAM", message="'store_id' is required.")
        if not isinstance(records, list) or not records:
            return self._failure(code="MISSING_PARAM", message="'records' must be a non-empty array.")

        if agent_context.dept_id is None:
            return self._failure(
                code="MISSING_CONTEXT",
                message="No active department in session context. Set a department before inserting records.",
            )

        store = await datastore_service.get_store(
            session, agent_context.org_id, agent_context.user_id, uuid.UUID(store_id),
            dept_id=agent_context.dept_id,
        )
        inserted = await datastore_service.bulk_insert_records(session, store, records)
        ms = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"inserted": len(inserted), "records": [_record_to_dict(r) for r in inserted]},
            execution_time_ms=ms,
        )

    async def _handle_update_record(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        from src.shared.services import datastore_service

        store_id = params.get("store_id")
        record_id = params.get("record_id")
        data = params.get("data")
        if not store_id:
            return self._failure(code="MISSING_PARAM", message="'store_id' is required.")
        if not record_id:
            return self._failure(code="MISSING_PARAM", message="'record_id' is required.")
        if data is None:
            return self._failure(code="MISSING_PARAM", message="'data' is required.")

        if agent_context.dept_id is None:
            return self._failure(
                code="MISSING_CONTEXT",
                message="No active department in session context. Set a department before updating records.",
            )

        store = await datastore_service.get_store(
            session, agent_context.org_id, agent_context.user_id, uuid.UUID(store_id),
            dept_id=agent_context.dept_id,
        )
        record = await datastore_service.update_record(session, store, uuid.UUID(record_id), data)
        ms = int((time.monotonic() - start) * 1000)
        return self._success(data=_record_to_dict(record), execution_time_ms=ms)

    async def _handle_delete_record(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        from src.shared.services import datastore_service

        store_id = params.get("store_id")
        record_id = params.get("record_id")
        if not store_id:
            return self._failure(code="MISSING_PARAM", message="'store_id' is required.")
        if not record_id:
            return self._failure(code="MISSING_PARAM", message="'record_id' is required.")

        if agent_context.dept_id is None:
            return self._failure(
                code="MISSING_CONTEXT",
                message="No active department in session context. Set a department before deleting records.",
            )

        # Visibility check
        await datastore_service.get_store(
            session, agent_context.org_id, agent_context.user_id, uuid.UUID(store_id),
            dept_id=agent_context.dept_id,
        )
        await datastore_service.delete_record(session, uuid.UUID(store_id), uuid.UUID(record_id))
        ms = int((time.monotonic() - start) * 1000)
        return self._success(data={"deleted": True, "record_id": record_id}, execution_time_ms=ms)


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def _store_to_dict(store) -> dict:
    return {
        "id": str(store.id),
        "org_id": str(store.org_id),
        "dept_id": str(store.dept_id) if store.dept_id else None,
        "created_by": str(store.created_by) if store.created_by else None,
        "name": store.name,
        "description": store.description,
        "schema": store.schema,
        "visibility": store.visibility,
        "max_records": store.max_records,
        "record_count": store.record_count,
        "is_active": store.is_active,
        "created_at": store.created_at.isoformat() if store.created_at else None,
        "updated_at": store.updated_at.isoformat() if store.updated_at else None,
    }


def _record_to_dict(record) -> dict:
    return {
        "id": str(record.id),
        "datastore_id": str(record.datastore_id),
        "data": record.data,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }
