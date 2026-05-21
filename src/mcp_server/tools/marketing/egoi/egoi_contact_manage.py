"""gSage AI — E-goi contact bulk management (approval-gated).

Handles destructive and high-volume contact operations:

- ``delete_one`` / ``delete_many``
- ``activate`` / ``deactivate`` / ``unsubscribe`` for arbitrary batch sizes
- ``attach_tag`` / ``detach_tag`` for arbitrary batch sizes
- ``forget`` (GDPR-style erase) for one or many contacts
- ``import_json`` — array of contact objects via the bulk-import endpoint
- ``import_csv`` — load a stored CSV file, map columns to E-goi fields
  and chunk the upload to respect the 20 MB request-body cap

Every invocation requires user approval and is executed in the
background. The original ``params`` payload (including ``reason``) is
captured in the audit log.

Permission: ``egoi:write``, ``egoi:delete``
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.core.csv.csv_loader import CSVAccessError, load_csv
from src.mcp_server.tools.marketing.egoi import _csv_import, _query as Q
from src.mcp_server.tools.marketing.egoi._client import EgoiClient, EgoiError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


_BULK_ACTIONS = (
    "delete_one",
    "delete_many",
    "activate",
    "deactivate",
    "unsubscribe",
    "forget",
    "attach_tag",
    "detach_tag",
    "import_json",
    "import_csv",
)


class EgoiContactManageTool(BaseTool):
    """Approval-gated, bulk-capable contact management.

    See module docstring for the full action list.
    """

    name: ClassVar[str] = "egoi_contact_manage"
    config_namespace: ClassVar[str] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Bulk E-goi contact operations (delete, mass status change, "
        "tag, forget, JSON/CSV bulk import). Requires approval."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:write", "egoi:delete"]

    rate_limit_per_minute: ClassVar[int] = 5
    timeout_seconds: ClassVar[int] = 900
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True
    always_background: ClassVar[bool] = True

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.EGOI_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.EGOI_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "list_id": "list_id",
        "contact_id": "contact_id",
        "contact_ids": "contact_ids",
        "tag_id": "tag_id",
        "file_id": "file_id",
        "email_column": "email_column",
        "field_mapping": "field_mapping",
        "reason": "reason",
    }
    audit_output: ClassVar[bool] = True

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action", "list_id", "reason"],
        "properties": {
            "action": {"type": "string", "enum": list(_BULK_ACTIONS)},
            "list_id": {"type": "integer", "minimum": 1},
            "reason": {
                "type": "string",
                "minLength": 5,
                "description": (
                    "Human-readable justification recorded in the audit "
                    "log. Required by the approval flow."
                ),
            },
            "contact_id": {
                **Q.CONTACT_ID_SCHEMA,
                "description": (
                    "Target id for *_one actions. Modern E-goi lists "
                    "use a 10-char hex hash; legacy lists may still use "
                    "an integer id."
                ),
            },
            "contact_ids": {
                "type": "array",
                "items": Q.CONTACT_ID_SCHEMA,
                "minItems": 1,
                "maxItems": Q.HARD_MAX_ROWS,
                "description": (
                    "Contact ids for bulk activate/deactivate/forget/"
                    "delete_many/attach_tag/detach_tag. Each item is an "
                    "integer or 10-char hex hash."
                ),
            },
            "tag_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Tag id for attach_tag/detach_tag.",
            },
            "contacts": {
                "type": "array",
                "items": {"type": "object"},
                "maxItems": 50000,
                "description": (
                    "Inline contact objects for action='import_json'. Each "
                    "object should follow the E-goi shape: "
                    "{'base': {...}, 'extra': {...}}."
                ),
            },
            "file_id": {
                "type": "string",
                "description": "GSageFile id of the CSV for action='import_csv'.",
            },
            "email_column": {
                "type": "string",
                "description": "CSV column that holds the email (action='import_csv').",
            },
            "field_mapping": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Map of CSV column -> E-goi field. Recognised base "
                    "fields: email, first_name, last_name, cellphone, "
                    "telephone, lang, birth_date, etc. Numeric strings "
                    "are treated as extra-field ids."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["insert", "update", "upsert"],
                "default": "upsert",
                "description": "Bulk-import merge strategy.",
            },
            "compare_field": {
                "type": "string",
                "default": "email",
                "description": "Field used to deduplicate bulk-import rows.",
            },
        },
        "additionalProperties": False,
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        action = str(params.get("action") or "").strip()
        list_id = params.get("list_id")
        if action not in _BULK_ACTIONS:
            return self._failure(
                "VALIDATION_ERROR",
                f"Unknown action '{action}'. One of {list(_BULK_ACTIONS)}.",
            )
        if not isinstance(list_id, int) or list_id <= 0:
            return self._failure("VALIDATION_ERROR", "'list_id' must be a positive integer")

        try:
            async with Q.build_client(config) as client:
                result = await self._dispatch(client, agent_context, action, list_id, params)
        except EgoiError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code,
                str(exc),
                retryable=Q.is_retryable_error(exc),
                execution_time_ms=elapsed,
            )
        except CSVAccessError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "FILE_NOT_FOUND", str(exc), execution_time_ms=elapsed
            )
        except ValueError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("VALIDATION_ERROR", str(exc), execution_time_ms=elapsed)
        except _csv_import.CSVImportError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("CSV_MAPPING_ERROR", str(exc), execution_time_ms=elapsed)
        except Exception as exc:  # noqa: BLE001
            log.exception("egoi_contact_manage: unexpected error (%s)", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {"action": action, "list_id": list_id, **result},
            execution_time_ms=elapsed,
        )

    # ── Dispatcher ─────────────────────────────────────────────────────

    async def _dispatch(
        self,
        client: EgoiClient,
        agent_context: AgentContext,
        action: str,
        list_id: int,
        params: dict,
    ) -> dict:
        if action == "delete_one":
            contact_id = Q.normalize_contact_id(params.get("contact_id"))
            # Forget single contact = E-goi's GDPR-erase semantics
            payload = await client.action_forget_contacts(
                list_id=list_id, body={"contacts": [contact_id]}
            )
            return {"deleted_contact_id": contact_id, "result": payload}

        if action == "delete_many" or action == "forget":
            ids = self._require_ids(params)
            payload = await client.action_forget_contacts(
                list_id=list_id, body={"contacts": ids}
            )
            return {"forgotten_contact_ids": ids, "result": payload}

        if action == "activate":
            ids = self._require_ids(params)
            payload = await client.action_activate_contacts(
                list_id=list_id,
                body={"type": "contacts", "contacts": ids},
            )
            return {"contact_ids": ids, "result": payload}

        if action == "deactivate":
            ids = self._require_ids(params)
            payload = await client.action_deactivate_contacts(
                list_id=list_id,
                body={"type": "contacts", "contacts": ids},
            )
            return {"contact_ids": ids, "result": payload}

        if action == "unsubscribe":
            ids = self._require_ids(params)
            # E-goi unsubscribe endpoint accepts one contact_id at a time —
            # fan out across the list and report partial failures.
            results: list[dict] = []
            for cid in ids:
                try:
                    res = await client.action_unsubscribe_contact(
                        list_id=list_id, body={"contact_id": cid}
                    )
                    results.append({"contact_id": cid, "ok": True, "response": res})
                except EgoiError as exc:
                    results.append(
                        {"contact_id": cid, "ok": False, "error": str(exc)}
                    )
            return {"results": results}

        if action in ("attach_tag", "detach_tag"):
            ids = self._require_ids(params)
            tag_id = params.get("tag_id")
            if not isinstance(tag_id, int) or tag_id <= 0:
                raise ValueError(f"'tag_id' is required for {action}")
            body = {"tag_id": int(tag_id), "contacts": ids}
            if action == "attach_tag":
                payload = await client.action_attach_tag(list_id=list_id, body=body)
            else:
                payload = await client.action_detach_tag(list_id=list_id, body=body)
            return {"contact_ids": ids, "tag_id": tag_id, "result": payload}

        if action == "import_json":
            contacts = params.get("contacts") or []
            if not isinstance(contacts, list) or not contacts:
                raise ValueError("'contacts' array is required for import_json")
            mode = str(params.get("mode") or "upsert")
            compare_field = str(params.get("compare_field") or "email")
            return await self._run_bulk_import(
                client,
                list_id=list_id,
                contacts=[c for c in contacts if isinstance(c, dict)],
                mode=mode,
                compare_field=compare_field,
            )

        if action == "import_csv":
            file_id = (params.get("file_id") or "").strip()
            email_column = (params.get("email_column") or "").strip()
            field_mapping = params.get("field_mapping") or {}
            if not file_id:
                raise ValueError("'file_id' is required for import_csv")
            if not email_column:
                raise ValueError("'email_column' is required for import_csv")
            if not isinstance(field_mapping, dict):
                raise ValueError("'field_mapping' must be an object")
            df, csv_meta = await load_csv(self, agent_context, file_id)
            contacts = _csv_import.parse_csv_to_contacts(
                df,
                email_column=email_column,
                field_mapping={str(k): str(v) for k, v in field_mapping.items()},
            )
            mode = str(params.get("mode") or "upsert")
            compare_field = str(params.get("compare_field") or "email")
            result = await self._run_bulk_import(
                client,
                list_id=list_id,
                contacts=contacts,
                mode=mode,
                compare_field=compare_field,
            )
            result.update(
                {"csv_file_id": file_id, "csv_rows_read": int(df.height)}
            )
            return result

        raise ValueError(f"Unhandled action '{action}'")

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _require_ids(params: dict) -> list[Any]:
        ids = params.get("contact_ids")
        return Q.normalize_contact_ids(ids)

    @staticmethod
    async def _run_bulk_import(
        client: EgoiClient,
        *,
        list_id: int,
        contacts: list[dict],
        mode: str,
        compare_field: str,
    ) -> dict:
        if not contacts:
            return {"contacts_total": 0, "chunks": [], "query_ids": []}
        chunks_meta: list[dict] = []
        query_ids: list[str] = []
        for idx, payload in enumerate(
            _csv_import.chunk_contacts(
                contacts, mode=mode, compare_field=compare_field
            )
        ):
            response = await client.action_import_bulk(
                list_id=list_id, body=payload
            )
            chunk_count = len(payload.get("contacts") or [])
            query_id: Optional[str] = None
            if isinstance(response, dict):
                query_id = (
                    response.get("query_id")
                    or response.get("import_id")
                    or response.get("id")
                )
            chunks_meta.append(
                {
                    "chunk_index": idx,
                    "contacts": chunk_count,
                    "query_id": query_id,
                    "response": response,
                }
            )
            if query_id:
                query_ids.append(str(query_id))
        return {
            "contacts_total": len(contacts),
            "chunks": chunks_meta,
            "query_ids": query_ids,
        }
