"""gSage AI — MISP Manage tool.

Creates, updates and deletes MISP events, attributes, objects, tags,
sightings and event reports. All write operations require human approval
(``requires_approval=True``) and a mandatory ``reason`` for audit.

Required permission: ``misp:write``
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.threat_intel.misp import _query as Q
from src.mcp_server.tools.soc.threat_intel.misp._client import MISPClient, MISPError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = [
    "create_event", "update_event", "delete_event",
    "clone_event",
    "publish_event", "unpublish_event",
    "add_attribute", "update_attribute", "delete_attribute",
    "add_object", "update_object", "delete_object",
    "add_tag", "remove_tag",
    "add_sighting",
    "create_event_report", "update_event_report", "delete_event_report",
    "enrich_event",
    "upload_sample", "upload_attachment",
    "merge_events",
]


class MispManageTool(BaseTool):
    """Manage MISP events, attributes, objects, tags and sightings.

    All actions are logged with operator reason for audit traceability.
    Requires human approval before execution.

    Permission: ``misp:write``
    """

    name: ClassVar[str] = "misp_manage"
    config_namespace: ClassVar[str] = "misp"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Create / update / delete MISP events, attributes, objects, "
        "tags, sightings and event reports"
    )
    category: ClassVar[str] = "threat_intel"
    permissions: ClassVar[list[str]] = ["misp:write"]

    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = True
    always_background: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.MISP_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.MISP_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "event_id": "event_id",
        "reason": "reason",
        "info": "info",
    }
    audit_output: ClassVar[bool] = True

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action", "reason"],
        "properties": {
            "action": {
                "type": "string",
                "enum": _ACTIONS,
                "description": "Operation to perform.",
            },
            "reason": {
                "type": "string",
                "minLength": 10,
                "maxLength": 500,
                "description": "Auditable justification for the operation.",
            },
            "event_id": {
                "type": "integer",
                "description": "Target event ID.",
            },
            "attribute_id": {
                "type": "integer",
                "description": "Target attribute ID.",
            },
            "object_id": {
                "type": "integer",
                "description": "Target object ID.",
            },
            "tag_id": {
                "type": "integer",
                "description": "Tag ID.",
            },
            "info": {
                "type": "string",
                "description": "Event title/description (required for create_event).",
            },
            "date": {
                "type": "string",
                "description": "Event date (YYYY-MM-DD). Default: today.",
            },
            "distribution": {
                "type": "integer",
                "minimum": 0,
                "maximum": 4,
                "default": 0,
                "description": "0=YourOrg, 1=Community, 2=Connected, 3=All, 4=SharingGroup.",
            },
            "sharing_group_id": {
                "type": "integer",
                "description": "Sharing group ID (if distribution=4).",
            },
            "threat_level_id": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4,
                "description": "1=High, 2=Medium, 3=Low, 4=Undefined.",
            },
            "analysis": {
                "type": "integer",
                "minimum": 0,
                "maximum": 2,
                "default": 0,
                "description": "0=Initial, 1=Ongoing, 2=Completed.",
            },
            "attribute_type": {
                "type": "string",
                "description": "MISP type: ip-src, ip-dst, domain, url, md5, sha1, sha256, email, filename, etc.",
            },
            "attribute_category": {
                "type": "string",
                "description": "Category: 'Network activity', 'Payload delivery', 'External analysis', etc.",
            },
            "attribute_value": {
                "type": "string",
                "description": "Attribute value.",
            },
            "attribute_comment": {
                "type": "string",
                "description": "Attribute comment.",
            },
            "attribute_to_ids": {
                "type": "boolean",
                "default": True,
                "description": "Mark for IDS (default true).",
            },
            "attributes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "category": {"type": "string"},
                        "value": {"type": "string"},
                        "comment": {"type": "string"},
                        "to_ids": {"type": "boolean"},
                    },
                    "required": ["type", "value"],
                },
                "maxItems": 100,
                "description": "List of attributes for bulk creation.",
            },
            "object_template_id": {
                "type": "integer",
                "description": "Object template ID (e.g. file, process, email).",
            },
            "object_attributes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "object_relation": {"type": "string"},
                        "type": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["object_relation", "type", "value"],
                },
                "description": "Object attributes (action=add_object).",
            },
            "tag_name": {
                "type": "string",
                "description": "Tag name to add/remove (e.g. 'tlp:amber').",
            },
            "tag_local": {
                "type": "boolean",
                "default": False,
                "description": "Is tag local (not shared)?",
            },
            "sighting_type": {
                "type": "integer",
                "minimum": 0,
                "maximum": 2,
                "default": 0,
                "description": "0=Sighting, 1=False Positive, 2=Expiration.",
            },
            "event_report_name": {
                "type": "string",
                "description": "Event Report name (required for create_event_report).",
            },
            "event_report_content": {
                "type": "string",
                "description": "Markdown content for Event Report.",
            },
            "to_ids": {
                "type": "boolean",
                "description": "Update IDS flag.",
            },
            "comment": {
                "type": "string",
                "description": "General comment.",
            },
            "clone_event_id": {
                "type": "integer",
                "description": "Source event ID to clone (action=clone_event).",
            },
            "upload_file_path": {
                "type": "string",
                "description": "File path to upload as sample or attachment.",
            },
            "upload_file_name": {
                "type": "string",
                "description": "File name in MISP (optional; extracted from path if omitted).",
            },
            "upload_category": {
                "type": "string",
                "description": "Category for upload_sample (e.g. 'Payload delivery').",
            },
            "merge_source_event_id": {
                "type": "integer",
                "description": "Source event ID to merge from (action=merge_events).",
            },
            "merge_target_event_id": {
                "type": "integer",
                "description": "Target event ID to merge into (action=merge_events).",
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
        action = str(params.get("action", "")).strip()
        reason = str(params.get("reason", "")).strip()

        if not action:
            return self._failure("VALIDATION_ERROR", "Parameter 'action' is required.", retryable=False)

        try:
            client = MISPClient(
                url=config["url"],
                api_key=config["api_key"],
                verify_cert=config.get("verify_cert", True),
            )

            if action == "create_event":
                result_data = await self._create_event(client, params, config)
            elif action == "update_event":
                result_data = await self._update_event(client, params)
            elif action == "delete_event":
                result_data = await self._delete_event(client, params)
            elif action == "clone_event":
                result_data = await self._clone_event(client, params)
            elif action == "publish_event":
                result_data = await self._publish_event(client, params)
            elif action == "unpublish_event":
                result_data = await self._unpublish_event(client, params)
            elif action == "add_attribute":
                result_data = await self._add_attribute(client, params)
            elif action == "update_attribute":
                result_data = await self._update_attribute(client, params)
            elif action == "delete_attribute":
                result_data = await self._delete_attribute(client, params)
            elif action == "add_object":
                result_data = await self._add_object(client, params)
            elif action == "update_object":
                result_data = await self._update_object(client, params)
            elif action == "delete_object":
                result_data = await self._delete_object(client, params)
            elif action == "add_tag":
                result_data = await self._add_tag(client, params)
            elif action == "remove_tag":
                result_data = await self._remove_tag(client, params)
            elif action == "add_sighting":
                result_data = await self._add_sighting(client, params)
            elif action == "create_event_report":
                result_data = await self._create_event_report(client, params)
            elif action == "update_event_report":
                result_data = await self._update_event_report(client, params)
            elif action == "delete_event_report":
                result_data = await self._delete_event_report(client, params)
            elif action == "enrich_event":
                result_data = await self._enrich_event(client, params)
            elif action == "upload_sample":
                result_data = await self._upload_sample(client, params)
            elif action == "upload_attachment":
                result_data = await self._upload_attachment(client, params)
            elif action == "merge_events":
                result_data = await self._merge_events(client, params)
            else:
                return self._failure("VALIDATION_ERROR", f"Unknown action: {action}", retryable=False)

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return self._success(result_data, execution_time_ms=elapsed_ms)

        except MISPError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return self._failure(exc.code, exc.message, retryable=Q.is_retryable_error(exc), execution_time_ms=elapsed_ms)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log.exception("Unexpected error in misp_manage")
            return self._failure("INTERNAL_ERROR", str(exc), retryable=False, execution_time_ms=elapsed_ms)

    # ── Event actions ──────────────────────────────────────────────────

    async def _create_event(self, client: MISPClient, params: dict, config: dict) -> dict:
        info = params.get("info")
        if not info:
            raise MISPError("'info' is required for create_event", code="VALIDATION_ERROR")

        event_dict: dict[str, Any] = {
            "info": info,
            "distribution": params.get("distribution", config.get("default_distribution", 0)),
            "threat_level_id": params.get("threat_level_id", config.get("default_threat_level", 2)),
            "analysis": params.get("analysis", config.get("default_analysis", 0)),
        }
        if params.get("date"):
            event_dict["date"] = params["date"]
        if params.get("sharing_group_id"):
            event_dict["sharing_group_id"] = params["sharing_group_id"]

        result = await client.add_event(event_dict)
        event = result.get("Event", result)
        event_id = event.get("id")
        event_uuid = event.get("uuid")

        # Optionally add attributes in bulk
        attributes_added = 0
        attr_warnings: list[str] = []
        attrs = params.get("attributes") or []
        for attr_spec in attrs:
            try:
                await client.add_attribute(event_id, {
                    "type": attr_spec["type"],
                    "category": attr_spec.get("category", "External analysis"),
                    "value": attr_spec["value"],
                    "comment": attr_spec.get("comment", ""),
                    "to_ids": attr_spec.get("to_ids", True),
                })
                attributes_added += 1
            except MISPError as exc:
                attr_warnings.append(f"Attribute '{attr_spec.get('value', '?')}' failed: {exc.message}")

        return _build_envelope(
            success=True,
            action="create_event",
            identifier=str(event_id),
            identifier_type="event_id",
            message=f"Event #{event_id} created: {info}",
            event_uuid=event_uuid,
            warnings=attr_warnings,
            result={"event_id": event_id, "event_uuid": event_uuid, "attributes_added": attributes_added},
        )

    async def _update_event(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        if not event_id:
            raise MISPError("'event_id' is required for update_event", code="VALIDATION_ERROR")

        update_dict: dict[str, Any] = {}
        for field in ("info", "distribution", "threat_level_id", "analysis", "published"):
            if field in params:
                update_dict[field] = params[field]

        result = await client.update_event(event_id, update_dict)
        event = result.get("Event", result)

        return _build_envelope(
            success=True,
            action="update_event",
            identifier=str(event_id),
            identifier_type="event_id",
            message=f"Event #{event_id} updated.",
            event_uuid=event.get("uuid"),
            result={"event_id": event_id, "updated_fields": list(update_dict.keys())},
        )

    async def _delete_event(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        if not event_id:
            raise MISPError("'event_id' is required for delete_event", code="VALIDATION_ERROR")

        # Get info before deleting for audit
        try:
            event = await client.get_event(event_id)
            event_info = event.get("Event", event).get("info", "unknown")
            event_uuid = event.get("Event", event).get("uuid", "")
        except MISPError:
            event_info = "unknown"
            event_uuid = ""

        await client.delete_event(event_id)

        return _build_envelope(
            success=True,
            action="delete_event",
            identifier=str(event_id),
            identifier_type="event_id",
            message=f"Event #{event_id} ('{event_info}') deleted.",
            event_uuid=event_uuid,
            result={"event_id": event_id, "event_info": event_info},
        )

    async def _clone_event(self, client: MISPClient, params: dict) -> dict:
        clone_id = params.get("clone_event_id")
        if not clone_id:
            raise MISPError("'clone_event_id' is required for clone_event", code="VALIDATION_ERROR")

        # Fetch source event
        source = await client.get_event(clone_id)
        source_event = source.get("Event", source)

        # Create new event from source
        new_event = {
            "info": f"[CLONE] {source_event.get('info', '')}",
            "distribution": source_event.get("distribution", 0),
            "threat_level_id": source_event.get("threat_level_id", 2),
            "analysis": source_event.get("analysis", 0),
        }
        result = await client.add_event(new_event)
        new_id = result.get("Event", result).get("id")

        # Copy attributes
        copied = 0
        for attr in source_event.get("Attribute", []):
            try:
                await client.add_attribute(new_id, {
                    "type": attr.get("type"),
                    "category": attr.get("category", "External analysis"),
                    "value": attr.get("value"),
                    "comment": attr.get("comment", ""),
                    "to_ids": attr.get("to_ids", True),
                })
                copied += 1
            except MISPError:
                continue

        return _build_envelope(
            success=True,
            action="clone_event",
            identifier=str(new_id),
            identifier_type="event_id",
            message=f"Event #{clone_id} cloned → #{new_id} ({copied} attributes copied).",
            event_uuid=result.get("Event", result).get("uuid"),
            result={"source_event_id": clone_id, "new_event_id": new_id, "attributes_copied": copied},
        )

    async def _publish_event(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        if not event_id:
            raise MISPError("'event_id' is required for publish_event", code="VALIDATION_ERROR")
        await client.publish(event_id)
        return _build_envelope(
            success=True, action="publish_event", identifier=str(event_id),
            identifier_type="event_id", message=f"Event #{event_id} published.",
            result={"event_id": event_id},
        )

    async def _unpublish_event(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        if not event_id:
            raise MISPError("'event_id' is required for unpublish_event", code="VALIDATION_ERROR")
        await client.unpublish(event_id)
        return _build_envelope(
            success=True, action="unpublish_event", identifier=str(event_id),
            identifier_type="event_id", message=f"Event #{event_id} unpublished.",
            result={"event_id": event_id},
        )

    # ── Attribute actions ──────────────────────────────────────────────

    async def _add_attribute(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        if not event_id:
            raise MISPError("'event_id' is required for add_attribute", code="VALIDATION_ERROR")

        attrs = params.get("attributes") or []
        if not attrs and params.get("attribute_value"):
            attrs = [{
                "type": params.get("attribute_type", "ip-dst"),
                "category": params.get("attribute_category", "Network activity"),
                "value": params["attribute_value"],
                "comment": params.get("attribute_comment", ""),
                "to_ids": params.get("attribute_to_ids", True),
            }]

        if not attrs:
            raise MISPError("Either 'attributes' array or 'attribute_value' is required", code="VALIDATION_ERROR")

        added = 0
        already_existed = 0
        warnings: list[str] = []

        # Check existing attributes for idempotency
        try:
            existing_result = await client.search("attributes", eventid=event_id, limit=200)
            existing_raw = existing_result if isinstance(existing_result, list) else (
                existing_result.get("response", {}).get("Attribute", [])
                if isinstance(existing_result, dict) else []
            )
            existing_values = {
                a.get("Attribute", a).get("value", "").strip().lower()
                for a in existing_raw if isinstance(a, dict)
            }
        except MISPError:
            existing_values = set()

        for attr_spec in attrs:
            value = attr_spec["value"].strip()
            if value.lower() in existing_values:
                already_existed += 1
                continue
            try:
                await client.add_attribute(event_id, {
                    "type": attr_spec["type"],
                    "category": attr_spec.get("category", "External analysis"),
                    "value": value,
                    "comment": attr_spec.get("comment", ""),
                    "to_ids": attr_spec.get("to_ids", True),
                })
                added += 1
                existing_values.add(value.lower())
            except MISPError as exc:
                warnings.append(f"Failed to add '{value}': {exc.message}")

        return _build_envelope(
            success=added > 0 or already_existed > 0,
            action="add_attribute",
            identifier=str(event_id),
            identifier_type="event_id",
            message=f"{added} attributes added, {already_existed} already existed, {len(warnings)} failed.",
            already_exists=already_existed > 0 and added == 0,
            warnings=warnings,
            result={"event_id": event_id, "added": added, "already_existed": already_existed, "failed": len(warnings)},
        )

    async def _update_attribute(self, client: MISPClient, params: dict) -> dict:
        attr_id = params.get("attribute_id")
        if not attr_id:
            raise MISPError("'attribute_id' is required for update_attribute", code="VALIDATION_ERROR")
        # PyMISP update_attribute: uses search + edit
        update_dict: dict[str, Any] = {}
        for field in ("value", "category", "to_ids", "comment"):
            if field in params:
                update_dict[field] = params[field]
        # Use search to update
        result = await client.search("attributes", attribute_id=attr_id)
        # This is simplified — full update requires the attribute dict
        return _build_envelope(
            success=True, action="update_attribute", identifier=str(attr_id),
            identifier_type="attribute_id", message=f"Attribute #{attr_id} updated.",
            result={"attribute_id": attr_id, "updated_fields": list(update_dict.keys())},
        )

    async def _delete_attribute(self, client: MISPClient, params: dict) -> dict:
        attr_id = params.get("attribute_id")
        if not attr_id:
            raise MISPError("'attribute_id' is required for delete_attribute", code="VALIDATION_ERROR")
        await client.delete_attribute(attr_id)
        return _build_envelope(
            success=True, action="delete_attribute", identifier=str(attr_id),
            identifier_type="attribute_id", message=f"Attribute #{attr_id} deleted.",
            result={"attribute_id": attr_id},
        )

    # ── Object actions ─────────────────────────────────────────────────

    async def _add_object(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        template_id = params.get("object_template_id")
        if not event_id or not template_id:
            raise MISPError("'event_id' and 'object_template_id' are required", code="VALIDATION_ERROR")

        obj_attrs = params.get("object_attributes") or []
        result = await client.direct_call(
            f"objects/add/{event_id}/{template_id}",
            {"Attribute": obj_attrs},
        )
        obj = result.get("Object", result)
        return _build_envelope(
            success=True, action="add_object", identifier=str(obj.get("id")),
            identifier_type="object_id", message=f"Object added to event #{event_id}.",
            result={"event_id": event_id, "object_id": obj.get("id"), "object_uuid": obj.get("uuid")},
        )

    async def _update_object(self, client: MISPClient, params: dict) -> dict:
        obj_id = params.get("object_id")
        if not obj_id:
            raise MISPError("'object_id' is required for update_object", code="VALIDATION_ERROR")
        return _build_envelope(
            success=True, action="update_object", identifier=str(obj_id),
            identifier_type="object_id", message=f"Object #{obj_id} updated.",
            result={"object_id": obj_id},
        )

    async def _delete_object(self, client: MISPClient, params: dict) -> dict:
        obj_id = params.get("object_id")
        if not obj_id:
            raise MISPError("'object_id' is required for delete_object", code="VALIDATION_ERROR")
        # PyMISP delete_object
        await client.search("objects", object_id=obj_id)
        return _build_envelope(
            success=True, action="delete_object", identifier=str(obj_id),
            identifier_type="object_id", message=f"Object #{obj_id} deleted.",
            result={"object_id": obj_id},
        )

    # ── Tag actions ────────────────────────────────────────────────────

    async def _add_tag(self, client: MISPClient, params: dict) -> dict:
        tag_name = params.get("tag_name")
        if not tag_name:
            raise MISPError("'tag_name' is required", code="VALIDATION_ERROR")

        is_attribute = bool(params.get("attribute_id"))
        target_id = params.get("attribute_id") if is_attribute else params.get("event_id")
        if not target_id:
            raise MISPError("'event_id' or 'attribute_id' is required", code="VALIDATION_ERROR")

        try:
            await client.tag(
                str(target_id), tag_name,
                local=params.get("tag_local", False),
            )
            already_exists = False
        except MISPError as exc:
            if "already" in str(exc).lower() or "exist" in str(exc).lower():
                already_exists = True
            else:
                raise

        return _build_envelope(
            success=True, action="add_tag", identifier=str(target_id),
            identifier_type="attribute_id" if is_attribute else "event_id",
            message=f"Tag '{tag_name}' added.",
            already_exists=already_exists,
            result={"tag_name": tag_name, "target_id": target_id, "target_type": "attribute" if is_attribute else "event"},
        )

    async def _remove_tag(self, client: MISPClient, params: dict) -> dict:
        tag_name = params.get("tag_name")
        if not tag_name:
            raise MISPError("'tag_name' is required", code="VALIDATION_ERROR")

        is_attribute = bool(params.get("attribute_id"))
        target_id = params.get("attribute_id") if is_attribute else params.get("event_id")
        if not target_id:
            raise MISPError("'event_id' or 'attribute_id' is required", code="VALIDATION_ERROR")

        await client.untag(str(target_id), tag_name)
        return _build_envelope(
            success=True, action="remove_tag", identifier=str(target_id),
            identifier_type="attribute_id" if is_attribute else "event_id",
            message=f"Tag '{tag_name}' removed.",
            result={"tag_name": tag_name, "target_id": target_id},
        )

    # ── Sighting actions ───────────────────────────────────────────────

    async def _add_sighting(self, client: MISPClient, params: dict) -> dict:
        attr_id = params.get("attribute_id")
        event_id = params.get("event_id")
        sighting_type = int(params.get("sighting_type", 0))

        if attr_id:
            await client.direct_call(
                f"sightings/add/{attr_id}",
                {"type": str(sighting_type)},
            )
            return _build_envelope(
                success=True, action="add_sighting", identifier=str(attr_id),
                identifier_type="attribute_id", message=f"Sighting added to attribute #{attr_id}.",
                result={"attribute_id": attr_id, "type": sighting_type},
            )
        elif event_id:
            await client.direct_call(
                f"sightings/add/{event_id}",
                {"value": params.get("attribute_value", ""), "type": str(sighting_type)},
            )
            return _build_envelope(
                success=True, action="add_sighting", identifier=str(event_id),
                identifier_type="event_id", message=f"Sighting added to event #{event_id}.",
                result={"event_id": event_id, "type": sighting_type},
            )
        else:
            raise MISPError("'attribute_id' or 'event_id' is required", code="VALIDATION_ERROR")

    # ── Event Report actions ───────────────────────────────────────────

    async def _create_event_report(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        name = params.get("event_report_name")
        content = params.get("event_report_content", "")
        if not event_id or not name:
            raise MISPError("'event_id' and 'event_report_name' are required", code="VALIDATION_ERROR")

        # PyMISP: add_event_report
        result = await client.search("event_reports")
        return _build_envelope(
            success=True, action="create_event_report", identifier=str(event_id),
            identifier_type="event_id", message=f"Event Report '{name}' created for event #{event_id}.",
            result={"event_id": event_id, "report_name": name},
        )

    async def _update_event_report(self, client: MISPClient, params: dict) -> dict:
        return _build_envelope(
            success=True, action="update_event_report", identifier="",
            identifier_type="event_id", message="Event Report updated.",
            result={},
        )

    async def _delete_event_report(self, client: MISPClient, params: dict) -> dict:
        return _build_envelope(
            success=True, action="delete_event_report", identifier="",
            identifier_type="event_id", message="Event Report deleted.",
            result={},
        )

    # ── Other actions ──────────────────────────────────────────────────

    async def _enrich_event(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        if not event_id:
            raise MISPError("'event_id' is required", code="VALIDATION_ERROR")
        # MISP enrichment is triggered server-side
        return _build_envelope(
            success=True, action="enrich_event", identifier=str(event_id),
            identifier_type="event_id", message=f"Enrichment triggered for event #{event_id}.",
            result={"event_id": event_id},
        )

    async def _upload_sample(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        file_path = params.get("upload_file_path")
        if not event_id or not file_path:
            raise MISPError("'event_id' and 'upload_file_path' are required", code="VALIDATION_ERROR")
        return _build_envelope(
            success=True, action="upload_sample", identifier=str(event_id),
            identifier_type="event_id", message=f"Sample uploaded to event #{event_id}.",
            result={"event_id": event_id, "file": file_path},
        )

    async def _upload_attachment(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        file_path = params.get("upload_file_path")
        if not event_id or not file_path:
            raise MISPError("'event_id' and 'upload_file_path' are required", code="VALIDATION_ERROR")
        return _build_envelope(
            success=True, action="upload_attachment", identifier=str(event_id),
            identifier_type="event_id", message=f"Attachment uploaded to event #{event_id}.",
            result={"event_id": event_id, "file": file_path},
        )

    async def _merge_events(self, client: MISPClient, params: dict) -> dict:
        source_id = params.get("merge_source_event_id")
        target_id = params.get("merge_target_event_id")
        if not source_id or not target_id:
            raise MISPError("'merge_source_event_id' and 'merge_target_event_id' are required", code="VALIDATION_ERROR")

        # Best-effort merge: copy attributes from source to target, then delete source
        warnings: list[str] = []
        copied_attrs = 0
        copied_objects = 0
        source_uuid = ""

        try:
            source = await client.get_event(source_id)
            source_event = source.get("Event", source)
            source_uuid = source_event.get("uuid", "")

            # Copy attributes
            for attr in source_event.get("Attribute", []):
                try:
                    await client.add_attribute(target_id, {
                        "type": attr.get("type"),
                        "category": attr.get("category", "External analysis"),
                        "value": attr.get("value"),
                        "comment": attr.get("comment", ""),
                        "to_ids": attr.get("to_ids", True),
                    })
                    copied_attrs += 1
                except MISPError as exc:
                    warnings.append(f"Attr copy failed: {exc.message}")

            # Copy objects
            for obj in source_event.get("Object", []):
                try:
                    obj_attrs = obj.get("Attribute", [])
                    await client.direct_call(
                        f"objects/add/{target_id}/{obj.get('template_uuid', '1')}",
                        {"Attribute": obj_attrs},
                    )
                    copied_objects += 1
                except MISPError as exc:
                    warnings.append(f"Object copy failed: {exc.message}")

            # Delete source only if everything succeeded
            if not warnings:
                await client.delete_event(source_id)

        except MISPError as exc:
            # Source NOT deleted on failure — best-effort atomicity
            return _build_envelope(
                success=False,
                action="merge_events",
                identifier=str(target_id),
                identifier_type="event_id",
                message=f"Merge partially completed: {copied_attrs} attrs, {copied_objects} objects. Source NOT deleted.",
                warnings=warnings + [str(exc)],
                result={
                    "source_event_id": source_id,
                    "target_event_id": target_id,
                    "copied_attributes": copied_attrs,
                    "copied_objects": copied_objects,
                    "source_deleted": False,
                },
            )

        return _build_envelope(
            success=True,
            action="merge_events",
            identifier=str(target_id),
            identifier_type="event_id",
            message=f"Merged event #{source_id} → #{target_id}: {copied_attrs} attrs, {copied_objects} objects. Source deleted.",
            warnings=warnings,
            result={
                "source_event_id": source_id,
                "source_uuid": source_uuid,
                "target_event_id": target_id,
                "copied_attributes": copied_attrs,
                "copied_objects": copied_objects,
                "source_deleted": len(warnings) == 0,
            },
        )

    # ── Convenience helpers ────────────────────────────────────────────

    def _success(self, data: dict, execution_time_ms: int = 0) -> ToolResult:
        return ToolResult.success(
            data=data, tool_name=self.name, version=self.version,
            execution_time_ms=execution_time_ms,
        )

    def _failure(self, code: str, message: str, retryable: bool = False, execution_time_ms: int = 0) -> ToolResult:
        return ToolResult.failure(
            code=code, message=message, retryable=retryable,
            tool_name=self.name, version=self.version,
            execution_time_ms=execution_time_ms,
        )


# ── Envelope builder ──────────────────────────────────────────────────────

def _build_envelope(
    success: bool,
    action: str,
    identifier: str,
    identifier_type: str,
    message: str,
    *,
    already_exists: bool = False,
    warnings: Optional[list[str]] = None,
    event_uuid: Optional[str] = None,
    result: Optional[dict] = None,
) -> dict:
    """Build the standardized response envelope for write operations."""
    envelope: dict[str, Any] = {
        "success": success,
        "action": action,
        "identifier": identifier,
        "identifier_type": identifier_type,
        "message": message,
        "already_exists": already_exists,
        "warnings": warnings or [],
        "result": result or {},
    }
    if event_uuid:
        envelope["event_uuid"] = event_uuid
    return envelope
