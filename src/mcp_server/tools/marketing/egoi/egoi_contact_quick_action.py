"""gSage AI — E-goi contact quick actions (small batches, no approval).

This tool exposes the *low-risk* subset of contact operations and
constrains every bulk-id input to at most ten items. For deletions,
imports or larger batches use :mod:`.egoi_contact_manage` (which is
approval-gated).

Permission: ``egoi:write``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.marketing.egoi import _query as Q
from src.mcp_server.tools.marketing.egoi._client import EgoiClient, EgoiError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


_QUICK_ACTIONS = (
    "create_one",
    "update_one",
    "activate",
    "deactivate",
    "unsubscribe",
    "attach_tag",
    "detach_tag",
)


class EgoiContactQuickActionTool(BaseTool):
    """Low-risk contact operations for small batches (max 10 ids per call).

    Available actions:

    - ``create_one`` — create a single contact in ``list_id`` from a
      ``contact`` object (must include ``email``).
    - ``update_one`` — PATCH a single contact (``list_id`` + ``contact_id``)
      with the fields in ``contact``.
    - ``activate`` / ``deactivate`` — toggle status for up to 10
      ``contact_ids`` inside ``list_id``.
    - ``unsubscribe`` — mark a single contact as unsubscribed.
    - ``attach_tag`` / ``detach_tag`` — apply or remove a single tag on
      up to 10 contacts inside ``list_id``.

    Permission: ``egoi:write``
    """

    name: ClassVar[str] = "egoi_contact_quick_action"
    config_namespace: ClassVar[str] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Quick low-risk E-goi contact actions (up to 10 ids per call). "
        "Use 'egoi_contact_manage' for delete, imports or larger batches."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:write"]

    rate_limit_per_minute: ClassVar[int] = 15
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

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
    }
    audit_output: ClassVar[bool] = True

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action", "list_id"],
        "properties": {
            "action": {"type": "string", "enum": list(_QUICK_ACTIONS)},
            "list_id": {"type": "integer", "minimum": 1},
            "contact_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Target contact id for *_one / unsubscribe actions.",
            },
            "contact_ids": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
                "maxItems": Q.QUICK_ACTION_MAX_ITEMS,
                "description": (
                    "Contact ids for activate/deactivate/attach_tag/"
                    f"detach_tag (max {Q.QUICK_ACTION_MAX_ITEMS})."
                ),
            },
            "tag_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Tag id for attach_tag/detach_tag.",
            },
            "contact": {
                "type": "object",
                "description": (
                    "Contact payload for create_one/update_one. Must "
                    "include 'email' for create_one. Recognised top-"
                    "level keys: email, first_name, last_name, "
                    "cellphone, telephone, lang, birth_date, status."
                ),
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
        if action not in _QUICK_ACTIONS:
            return self._failure(
                "VALIDATION_ERROR",
                f"Unknown action '{action}'. One of {list(_QUICK_ACTIONS)}.",
            )
        if not isinstance(list_id, int) or list_id <= 0:
            return self._failure("VALIDATION_ERROR", "'list_id' must be a positive integer")

        try:
            async with Q.build_client(config) as client:
                result = await self._dispatch(client, action, list_id, params)
        except EgoiError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code,
                str(exc),
                retryable=Q.is_retryable_error(exc),
                execution_time_ms=elapsed,
            )
        except ValueError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "VALIDATION_ERROR", str(exc), execution_time_ms=elapsed
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("egoi_contact_quick_action: unexpected error (%s)", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {"action": action, "list_id": list_id, **result},
            execution_time_ms=elapsed,
        )

    async def _dispatch(
        self,
        client: EgoiClient,
        action: str,
        list_id: int,
        params: dict,
    ) -> dict:
        contact = params.get("contact") or {}
        if action == "create_one":
            if not isinstance(contact, dict) or not contact.get("email"):
                raise ValueError("'contact.email' is required for create_one")
            payload = await client.create_contact(
                list_id=list_id, body={"base": dict(contact)}
            )
            return {"contact": Q.normalize_contact(payload) if isinstance(payload, dict) else {}}

        if action == "update_one":
            contact_id = params.get("contact_id")
            if not isinstance(contact_id, int) or contact_id <= 0:
                raise ValueError("'contact_id' is required for update_one")
            if not isinstance(contact, dict) or not contact:
                raise ValueError("'contact' fields are required for update_one")
            payload = await client.patch_contact(
                list_id=list_id,
                contact_id=int(contact_id),
                body={"base": dict(contact)},
            )
            return {"contact": Q.normalize_contact(payload) if isinstance(payload, dict) else {}}

        if action == "unsubscribe":
            contact_id = params.get("contact_id")
            if not isinstance(contact_id, int) or contact_id <= 0:
                raise ValueError("'contact_id' is required for unsubscribe")
            payload = await client.action_unsubscribe_contact(
                list_id=list_id,
                body={"contact_id": int(contact_id)},
            )
            return {"result": payload}

        ids = params.get("contact_ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValueError(f"'contact_ids' is required for action '{action}'")
        if len(ids) > Q.QUICK_ACTION_MAX_ITEMS:
            raise ValueError(
                f"contact_ids length {len(ids)} exceeds quick-action cap "
                f"({Q.QUICK_ACTION_MAX_ITEMS}). Use egoi_contact_manage."
            )
        clean_ids = [int(x) for x in ids]

        if action == "activate":
            payload = await client.action_activate_contacts(
                list_id=list_id,
                body={"type": "contacts", "contacts": clean_ids},
            )
            return {"contact_ids": clean_ids, "result": payload}

        if action == "deactivate":
            payload = await client.action_deactivate_contacts(
                list_id=list_id,
                body={"type": "contacts", "contacts": clean_ids},
            )
            return {"contact_ids": clean_ids, "result": payload}

        if action in ("attach_tag", "detach_tag"):
            tag_id = params.get("tag_id")
            if not isinstance(tag_id, int) or tag_id <= 0:
                raise ValueError(f"'tag_id' is required for {action}")
            body = {"tag_id": int(tag_id), "contacts": clean_ids}
            if action == "attach_tag":
                payload = await client.action_attach_tag(list_id=list_id, body=body)
            else:
                payload = await client.action_detach_tag(list_id=list_id, body=body)
            return {"contact_ids": clean_ids, "tag_id": tag_id, "result": payload}

        raise ValueError(f"Unhandled action '{action}'")
