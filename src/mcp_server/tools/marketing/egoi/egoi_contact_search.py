"""gSage AI — E-goi contact search tool (global + in-list scopes).

Permission: ``egoi:read``
"""

from __future__ import annotations

import csv
import logging
import os
import tempfile
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.marketing.egoi import _query as Q
from src.mcp_server.tools.marketing.egoi import _run
from src.mcp_server.tools.marketing.egoi import _tags
from src.mcp_server.tools.marketing.egoi._client import EgoiClient, EgoiError
from src.mcp_server.tools.result_export import (
    IncrementalSummary,
    _csv_value,
    _safe_filename_prefix,
    store_export_artifact,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class EgoiContactSearchTool(BaseTool):
    """Search E-goi contacts globally or inside a specific mailing list.

    Two scopes:

    - ``global`` — uses ``GET /contacts-search`` to locate a contact by
      its email address across every list of the tenant.
    - ``list`` — uses ``GET /lists/{id}/contacts`` (optionally
      ``/segment/{seg_id}``) to enumerate contacts in a list/segment,
      with rich filtering.

    Permission: ``egoi:read``
    """

    name: ClassVar[str] = "egoi_contact_search"
    config_namespace: ClassVar[Optional[str]] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Search E-goi contacts. Use scope='global' to locate a contact "
        "by email across all lists, or scope='list' to enumerate "
        "contacts inside a specific list/segment."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:read"]

    rate_limit_per_minute: ClassVar[int] = 30
    # Sync timeout doubles as the auto-fallback trigger when
    # ``background_threshold_seconds`` is set. We keep it under the chat
    # layer's tool timeout so the agent gets a 'background' status
    # instead of a timeout error.
    timeout_seconds: ClassVar[int] = 120
    background_threshold_seconds: ClassVar[Optional[int]] = 120
    # Large lists (50k+ contacts) need plenty of room when paging at 200/page
    # with occasional RemoteDisconnected retries.  30 min keeps us safely below
    # Celery's hard task limit while covering realistic tenant volumes.
    background_timeout_seconds: ClassVar[Optional[int]] = 1800
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.EGOI_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.EGOI_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {}
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "required": ["scope"],
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["global", "list"],
                "description": (
                    "'global' searches by email across the whole tenant; "
                    "'list' enumerates contacts inside the given list."
                ),
            },
            "contact": {
                "type": "string",
                "description": (
                    "Email or phone (E.164) to locate. REQUIRED when "
                    "scope='global'."
                ),
            },
            "list_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Target list. REQUIRED when scope='list'.",
            },
            "segment_id": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Optional segment inside the list (scope='list')."
                ),
            },
            "status": {
                "type": "string",
                "enum": ["active", "inactive", "removed", "unconfirmed"],
                "description": "Optional contact status filter (scope='list').",
            },
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": Q.HARD_MAX_ROWS,
                "default": Q.DEFAULT_MAX_ROWS,
            },
            "export_csv": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Persist all rows as a CSV file artifact. CSV is the "
                    "only supported export format for tabular results."
                ),
            },
            "resolve_tags": {
                "type": "boolean",
                "default": True,
                "description": (
                    "When true, each row's 'tags' field is enriched into "
                    "[{tag_id, name}, ...] by resolving tag ids against "
                    "GET /tags. Adds one cached lookup per execution. "
                    "Set false for very large enumerations where the "
                    "raw id list is acceptable."
                ),
            },
            "tag_id": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Filter to contacts carrying this tag id (scope='list' "
                    "only). The E-goi API has no native 'contacts by tag' "
                    "endpoint, so the tool enumerates ALL contacts in the "
                    "list and filters client-side. Forces background "
                    "execution and emits TWO CSV artifacts: the raw list "
                    "dump and the filtered subset."
                ),
            },
            "tag_name": {
                "type": "string",
                "description": (
                    "Same as tag_id but resolved by name (case-insensitive "
                    "exact match). Mutually exclusive with tag_id."
                ),
            },
        },
        "additionalProperties": False,
    }

    async def should_run_background(self, params: dict, config: dict) -> bool:
        # Tag-filter requires a full list dump; always go background.
        if (
            (params.get("scope") or "").strip().lower() == "list"
            and (params.get("tag_id") is not None or params.get("tag_name"))
        ):
            return True
        # E-goi contact pages are ~200 rows and the API sustains roughly
        # 300-500 rows/sec. Dispatch immediately when the requested
        # batch is large or a sizeable export was requested.
        if _run.should_background_for_size(
            params,
            rows_threshold=5000,
            export_rows_threshold=2000,
        ):
            return True
        return await super().should_run_background(params, config)

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        scope = str(params.get("scope") or "").strip()
        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        resolve_tags_flag = bool(params.get("resolve_tags", True))

        # ── Tag-filter validation (scope='list' only) ─────────────────
        tag_id_raw = params.get("tag_id")
        tag_name_raw = (params.get("tag_name") or "").strip()
        tag_filter_requested = tag_id_raw is not None or bool(tag_name_raw)
        if tag_filter_requested and scope != "list":
            return self._failure(
                "VALIDATION_ERROR",
                "tag_id/tag_name filter requires scope='list'",
            )
        if tag_id_raw is not None and tag_name_raw:
            return self._failure(
                "VALIDATION_ERROR",
                "'tag_id' and 'tag_name' are mutually exclusive",
            )
        if tag_id_raw is not None and (
            not isinstance(tag_id_raw, int) or tag_id_raw <= 0
        ):
            return self._failure(
                "VALIDATION_ERROR",
                "'tag_id' must be a positive integer",
            )

        if scope == "global":
            contact = (params.get("contact") or "").strip()
            if not contact:
                return self._failure(
                    "VALIDATION_ERROR",
                    "scope='global' requires 'contact' (email or phone)",
                )

            async def _fetch_global(client: EgoiClient) -> tuple[list[dict], Optional[int]]:
                payload = await client.search_contacts(contact=contact)
                tag_index = (
                    await _tags.get_tag_index(client, org_id=agent_context.org_id)
                    if resolve_tags_flag
                    else None
                )
                rows = [
                    Q.normalize_contact(x, tag_index=tag_index)
                    for x in Q.unwrap_items(payload)
                ]
                if not rows and isinstance(payload, dict):
                    # search_contacts may return a single object
                    rows = (
                        [Q.normalize_contact(payload, tag_index=tag_index)]
                        if payload.get("contact_id")
                        else []
                    )
                return rows[:max_rows], Q.total_items(payload)

            return await _run.run_search(
                self,
                agent_context=agent_context,
                config=config,
                fetcher=_fetch_global,
                filename_prefix="egoi_contact_search_global",
                export_csv=bool(params.get("export_csv", False)),
                summary_group_by=["status", "language"],
                extra_data={"scope": "global", "contact": contact},
                operation_label="egoi contact_search global",
            )

        if scope == "list":
            list_id = params.get("list_id")
            if not isinstance(list_id, int) or list_id <= 0:
                return self._failure(
                    "VALIDATION_ERROR",
                    "scope='list' requires a positive integer 'list_id'",
                )
            segment_id_raw = params.get("segment_id")
            segment_id = (
                segment_id_raw
                if isinstance(segment_id_raw, int) and segment_id_raw > 0
                else None
            )
            status = (params.get("status") or "").strip() or None

            if tag_filter_requested:
                return await self._run_list_with_tag_filter(
                    agent_context=agent_context,
                    config=config,
                    list_id=list_id,
                    segment_id=segment_id,
                    status=status,
                    max_rows=max_rows,
                    tag_id_param=tag_id_raw if isinstance(tag_id_raw, int) else None,
                    tag_name_param=tag_name_raw or None,
                )

            async def _fetch_list(client: EgoiClient) -> tuple[list[dict], Optional[int]]:
                tag_index = (
                    await _tags.get_tag_index(client, org_id=agent_context.org_id)
                    if resolve_tags_flag
                    else None
                )

                def _normalise(item: Any) -> dict:
                    return Q.normalize_contact(item, tag_index=tag_index)

                if segment_id is not None:
                    segment_id_value = segment_id

                    async def page(offset: int, limit: int):
                        return await client.get_all_contacts_by_segment(
                            list_id=list_id,
                            segment_id=segment_id_value,
                            offset=offset,
                            limit=limit,
                        )
                else:
                    async def page(offset: int, limit: int):
                        kwargs: dict[str, Any] = {
                            "list_id": list_id,
                            "offset": offset,
                            "limit": limit,
                        }
                        if status is not None:
                            kwargs["status"] = status
                        return await client.get_all_contacts(**kwargs)

                rows, server_total = await Q.iter_all_pages(
                    page,
                    max_rows=max_rows,
                    normaliser=_normalise,
                )
                # Tag the list_id on every row for downstream clarity.
                for r in rows:
                    r.setdefault("list_id", list_id)
                return rows, server_total

            export_csv_flag = bool(params.get("export_csv", False))

            # Streaming path: very large enumerations would OOM the worker
            # if we accumulated every row + materialised the full CSV in
            # memory. Above STREAM_THRESHOLD, persist rows to a tempfile
            # CSV one page at a time.
            STREAM_THRESHOLD = 5000
            use_streaming = export_csv_flag and max_rows >= STREAM_THRESHOLD

            if use_streaming:
                async def _stream_list(client: EgoiClient):
                    tag_index = (
                        await _tags.get_tag_index(client, org_id=agent_context.org_id)
                        if resolve_tags_flag
                        else None
                    )

                    def _normalise(item: Any) -> dict:
                        return Q.normalize_contact(item, tag_index=tag_index)

                    if segment_id is not None:
                        segment_id_value = segment_id

                        async def page(offset: int, limit: int):
                            return await client.get_all_contacts_by_segment(
                                list_id=list_id,
                                segment_id=segment_id_value,
                                offset=offset,
                                limit=limit,
                            )
                    else:
                        async def page(offset: int, limit: int):
                            kwargs: dict[str, Any] = {
                                "list_id": list_id,
                                "offset": offset,
                                "limit": limit,
                            }
                            if status is not None:
                                kwargs["status"] = status
                            return await client.get_all_contacts(**kwargs)

                    async for row, total in Q.iter_all_pages_stream(
                        page,
                        max_rows=max_rows,
                        normaliser=_normalise,
                    ):
                        row.setdefault("list_id", list_id)
                        yield row, total

                return await _run.run_search_streaming(
                    self,
                    agent_context=agent_context,
                    config=config,
                    streamer=_stream_list,
                    filename_prefix="egoi_contact_search_list",
                    csv_columns=[
                        "contact_id",
                        "list_id",
                        "status",
                        "email",
                        "first_name",
                        "last_name",
                        "cellphone",
                        "telephone",
                        "birth_date",
                        "language",
                        "created",
                        "updated",
                        "tags",
                        "extra",
                    ],
                    summary_group_by=["status", "language"],
                    extra_data={
                        "scope": "list",
                        "list_id": list_id,
                        "segment_id": segment_id,
                        "status": status,
                    },
                    operation_label="egoi contact_search list (stream)",
                )

            return await _run.run_search(
                self,
                agent_context=agent_context,
                config=config,
                fetcher=_fetch_list,
                filename_prefix="egoi_contact_search_list",
                export_csv=export_csv_flag,
                summary_group_by=["status", "language"],
                extra_data={
                    "scope": "list",
                    "list_id": list_id,
                    "segment_id": segment_id,
                    "status": status,
                },
                operation_label="egoi contact_search list",
            )

        return self._failure(
            "VALIDATION_ERROR",
            f"Unknown scope='{scope}'. Use 'global' or 'list'.",
        )

    # ── Tag-filtered list dump (dual-CSV) ───────────────────────────────
    async def _run_list_with_tag_filter(
        self,
        *,
        agent_context: AgentContext,
        config: dict,
        list_id: int,
        segment_id: Optional[int],
        status: Optional[str],
        max_rows: int,
        tag_id_param: Optional[int],
        tag_name_param: Optional[str],
    ) -> ToolResult:
        """Enumerate every contact in *list_id* and split into two CSVs.

        E-goi has no "contacts by tag" endpoint, so the tool downloads
        the full list (respecting *segment_id* / *status* filters) and
        builds two artifacts:

        * ``csv_file_raw`` — every contact returned by the API.
        * ``csv_file_filtered`` — only rows carrying the resolved tag.

        Both files are written one page at a time to a temp directory
        so memory stays flat regardless of list size. Always runs in
        background (see :meth:`should_run_background`).
        """
        t0 = time.monotonic()
        operation_label = "egoi contact_search list (tag-filter)"
        csv_columns = [
            "contact_id",
            "list_id",
            "status",
            "email",
            "first_name",
            "last_name",
            "cellphone",
            "telephone",
            "birth_date",
            "language",
            "created",
            "updated",
            "tags",
            "extra",
        ]
        preview_rows = Q.AGENT_PREVIEW_ROWS_EGOI

        try:
            async with Q.build_client(config) as client:
                # Resolve tag (always — needed for matching + readable name).
                tag_index = await _tags.get_tag_index(
                    client, org_id=agent_context.org_id
                )
                try:
                    target_tag_id = _tags.resolve_tag_value(
                        tag_id_param if tag_id_param is not None else tag_name_param,
                        index=tag_index,
                    )
                except ValueError as exc:
                    return self._failure("VALIDATION_ERROR", str(exc))
                target_tag_name = tag_index.by_id.get(target_tag_id) or str(
                    target_tag_id
                )

                # Page builder mirrors the regular list path.
                if segment_id is not None:
                    seg_id = segment_id

                    async def page(offset: int, limit: int):
                        return await client.get_all_contacts_by_segment(
                            list_id=list_id,
                            segment_id=seg_id,
                            offset=offset,
                            limit=limit,
                        )
                else:
                    async def page(offset: int, limit: int):
                        kwargs: dict[str, Any] = {
                            "list_id": list_id,
                            "offset": offset,
                            "limit": limit,
                        }
                        if status is not None:
                            kwargs["status"] = status
                        return await client.get_all_contacts(**kwargs)

                def _normalise(item: Any) -> dict:
                    return Q.normalize_contact(item, tag_index=tag_index)

                safe_prefix = _safe_filename_prefix(
                    f"egoi_contact_search_list_{list_id}_tag_{target_tag_id}"
                )
                ts = int(time.time())
                raw_name = f"{safe_prefix}_raw_{ts}.csv"
                filtered_name = f"{safe_prefix}_filtered_{ts}.csv"

                incsum = IncrementalSummary(
                    group_by=["status", "language"],
                    top_n=10,
                    sample_size=20,
                )
                preview: list[dict] = []
                rows_total = 0
                rows_matched = 0
                server_total: Optional[int] = None

                with tempfile.TemporaryDirectory(prefix="gsage-export-") as tmpdir:
                    raw_path = os.path.join(tmpdir, raw_name)
                    filtered_path = os.path.join(tmpdir, filtered_name)
                    # newline="" required by csv module for cross-platform output.
                    with open(raw_path, "w", encoding="utf-8", newline="") as raw_fh, \
                         open(filtered_path, "w", encoding="utf-8", newline="") as flt_fh:
                        raw_writer = csv.DictWriter(
                            raw_fh, fieldnames=csv_columns, extrasaction="ignore"
                        )
                        flt_writer = csv.DictWriter(
                            flt_fh, fieldnames=csv_columns, extrasaction="ignore"
                        )
                        raw_writer.writeheader()
                        flt_writer.writeheader()

                        async for row, total in Q.iter_all_pages_stream(
                            page, max_rows=max_rows, normaliser=_normalise,
                        ):
                            if server_total is None and isinstance(total, int):
                                server_total = total
                            row.setdefault("list_id", list_id)
                            rows_total += 1
                            csv_row = {
                                k: _csv_value(row.get(k)) for k in csv_columns
                            }
                            raw_writer.writerow(csv_row)

                            row_tags = row.get("tags") or []
                            is_match = any(
                                isinstance(t, dict) and t.get("tag_id") == target_tag_id
                                for t in row_tags
                            )
                            if is_match:
                                rows_matched += 1
                                flt_writer.writerow(csv_row)
                                incsum.add(row)
                                if len(preview) < preview_rows:
                                    preview.append(row)

                    # Upload both artifacts.
                    raw_artifact: Optional[dict] = None
                    filtered_artifact: Optional[dict] = None
                    if rows_total > 0:
                        with open(raw_path, "rb") as fh:
                            raw_artifact = await store_export_artifact(
                                tool=self,
                                agent_context=agent_context,
                                data=fh.read(),
                                filename=raw_name,
                                content_type="text/csv",
                            )
                    if rows_matched > 0:
                        with open(filtered_path, "rb") as fh:
                            filtered_artifact = await store_export_artifact(
                                tool=self,
                                agent_context=agent_context,
                                data=fh.read(),
                                filename=filtered_name,
                                content_type="text/csv",
                            )

                log.info(
                    "%s: list_id=%s tag_id=%s rows_total=%d rows_matched=%d server_total=%s",
                    operation_label, list_id, target_tag_id,
                    rows_total, rows_matched, server_total,
                )
        except EgoiError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code,
                str(exc),
                retryable=Q.is_retryable_error(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: unexpected error", operation_label)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        artifacts = {
            "csv_file_raw": raw_artifact,
            "csv_file_filtered": filtered_artifact,
            "csv_error": None,
            "json_file": None,
            "json_error": None,
        }
        agent_hint = (
            f"Downloaded {rows_total} contacts from list {list_id} and "
            f"filtered by tag '{target_tag_name}' (id={target_tag_id}): "
            f"{rows_matched} match(es). Two CSVs were produced — present "
            f"the download links to the user instead of enumerating rows "
            f"in chat."
        )
        raw_dl = (raw_artifact or {}).get("download_path") if isinstance(raw_artifact, dict) else None
        flt_dl = (filtered_artifact or {}).get("download_path") if isinstance(filtered_artifact, dict) else None
        if raw_dl:
            agent_hint += f" raw_download_path={raw_dl}"
        if flt_dl:
            agent_hint += f" filtered_download_path={flt_dl}"

        payload: dict[str, Any] = {
            "scope": "list",
            "list_id": list_id,
            "segment_id": segment_id,
            "status": status,
            "tag_id": target_tag_id,
            "tag_name": target_tag_name,
            "rows_total": rows_total,
            "rows_matched": rows_matched,
            "rows_fetched": rows_matched,  # preview reflects matched rows only
            "server_total_items": server_total,
            "rows_overflow": rows_matched > preview_rows,
            "rows": preview,
            "summary": incsum.finalize(),
            "artifacts": artifacts,
            "agent_hint": agent_hint,
        }
        return self._success(payload, execution_time_ms=elapsed)
