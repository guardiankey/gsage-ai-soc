"""gSage AI — Trellix EDR file hunt tool.

Convenience shortcut over :class:`trellix_edr_search` for the common case of
hunting files by name and/or hash on a subset of hosts.  The hash type is
auto-detected from the input length (32→MD5, 40→SHA1, 64→SHA256) so the
agent only needs to supply the value.

Permission: ``edr:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.trellix import _query as Q
from src.mcp_server.tools.soc.edr.trellix._artifacts import maybe_export
from src.mcp_server.tools.soc.edr.trellix._client import TrellixEDRError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class TrellixEdrSearchFilesTool(BaseTool):
    """Hunt files across endpoints by name, hash, or hostname filter.

    Examples:
        - ``hash="44d88612fea8a8f36de82e1278abb02f"`` → MD5 EICAR hash on every host.
        - ``file_name="powershell.exe"`` + ``hostname_contains="srv-"`` → matches
          on hosts whose name contains "srv-".
        - ``hash="da39a3ee5e6b4b0d3255bfef95601890afd80709"`` → SHA1 lookup.

    Permission: ``edr:read``
    """

    name: ClassVar[str] = "trellix_edr_search_files"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Search files across Trellix EDR endpoints by name and/or hash "
        "(MD5/SHA1/SHA256 auto-detected by length)"
    )
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["edr:read"]

    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 900
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    always_background: ClassVar[bool] = True

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.TRELLIX_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.TRELLIX_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {
        "target_entities": "hostname_equals",
    }
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "file_name": {
                "type": "string",
                "description": (
                    "Substring match on the file's full path (Files.full_name CONTAINS)."
                ),
            },
            "hash": {
                "type": "string",
                "description": (
                    "File hash (32 hex chars=MD5, 40=SHA1, 64=SHA256). "
                    "The tool auto-detects the algorithm from the length."
                ),
            },
            "hostname_contains": {
                "type": "string",
                "description": "Substring match on hostname (HostInfo.hostname CONTAINS).",
            },
            "hostname_equals": {
                "type": "string",
                "description": "Exact match on hostname (HostInfo.hostname EQUALS).",
            },
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": Q.HARD_MAX_ROWS,
                "default": Q.DEFAULT_MAX_ROWS,
            },
            "export_csv": {"type": "boolean", "default": False},
            "export_json": {"type": "boolean", "default": False},
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

        file_name = (params.get("file_name") or "").strip() or None
        raw_hash = (params.get("hash") or "").strip() or None
        hostname_contains = (params.get("hostname_contains") or "").strip() or None
        hostname_equals = (params.get("hostname_equals") or "").strip() or None

        if not any([file_name, raw_hash, hostname_contains, hostname_equals]):
            return self._failure(
                "INVALID_INPUT",
                "Provide at least one of: file_name, hash, hostname_contains, hostname_equals.",
            )

        hash_type: Optional[Q.HashType] = None
        hash_value: Optional[str] = None
        if raw_hash:
            detected = Q.detect_hash_type(raw_hash)
            if detected is None:
                return self._failure(
                    "INVALID_HASH",
                    f"'hash' is not a valid MD5/SHA1/SHA256 hex value (got length={len(raw_hash)}).",
                )
            hash_type, hash_value = detected

        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        export_csv = bool(params.get("export_csv", False))
        export_json = bool(params.get("export_json", False))

        payload = Q.build_files_payload(
            file_name=file_name,
            hash_type=hash_type,
            hash_value=hash_value,
            hostname_contains=hostname_contains,
            hostname_equals=hostname_equals,
        )

        try:
            async with Q.build_client(config) as client:
                query_id, rows, meta, truncated = await Q.run_search_pipeline(
                    client,
                    api_version="v1",
                    payload=payload,
                    max_rows=max_rows,
                )
        except TrellixEDRError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.status_code in (429, 500, 502, 503, 504)
            return self._failure(exc.code, str(exc), retryable=retryable, execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("trellix_edr_search_files: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        summary = Q.summarize(
            rows,
            group_by=[
                "HostInfo_hostname",
                "Files_sha1",
                "Files_sha256",
                "Files_md5",
                "Files_status",
            ],
        )
        artifacts = await maybe_export(
            self,
            rows=rows,
            export_csv=export_csv,
            export_json=export_json,
            filename_prefix=f"trellix_edr_files_{query_id}",
            agent_context=agent_context,
        )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {
                "query_id": query_id,
                "api_version": "v1",
                "criteria": {
                    "file_name": file_name,
                    "hash_type": hash_type,
                    "hash_value": hash_value,
                    "hostname_contains": hostname_contains,
                    "hostname_equals": hostname_equals,
                },
                "total_count": meta.get("total_count", len(rows)),
                "total_hosts": meta.get("total_hosts", 0),
                "truncated": truncated,
                "summary": summary,
                "rows": rows,
                "artifacts": artifacts,
            },
            execution_time_ms=elapsed,
        )
