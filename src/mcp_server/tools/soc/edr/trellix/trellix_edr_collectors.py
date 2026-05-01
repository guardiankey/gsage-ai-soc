"""gSage AI — Trellix EDR built-in collectors reference.

Pure local lookup over the static documentation we ship with the project
under :mod:`collectors/`. The agent uses this tool to discover which
collectors exist and to learn their fields and v1/v2 example queries
**before** calling :class:`trellix_edr_search` (which talks to the actual
Trellix API).

No network, no DB, no MinIO.

Permission: ``edr:read``
"""

from __future__ import annotations

import json
import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


# Pages that ship under collectors/ but are not actual collector schemas
# (overview / how-to articles converted by the same scraper).
_NON_COLLECTOR_PAGES = frozenset({
    "builtin_collectors",
    "collecting_device_data_for_realtime_search",
    "create_a_custom",
    "custom_collectors",
})

_COLLECTORS_DIR = Path(__file__).resolve().parent / "collectors"


@lru_cache(maxsize=1)
def _load_index() -> dict[str, dict[str, Any]]:
    """Load every JSON in ``collectors/`` (cached for the process lifetime).

    Returns a mapping ``lowered_name -> collector_data`` where
    ``lowered_name`` is the canonical collector ``name`` field from the JSON
    (e.g. ``"networkflow"``, ``"dnscache"``). Pages that belong to
    :data:`_NON_COLLECTOR_PAGES` or have no ``fields`` are skipped.
    """
    index: dict[str, dict[str, Any]] = {}
    if not _COLLECTORS_DIR.is_dir():
        log.warning("trellix_edr_collectors: directory missing: %s", _COLLECTORS_DIR)
        return index
    for path in sorted(_COLLECTORS_DIR.glob("*.json")):
        if path.stem in _NON_COLLECTOR_PAGES:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("trellix_edr_collectors: cannot parse %s: %s", path, exc)
            continue
        # Skip non-collector / malformed pages.
        if not isinstance(data, dict):
            continue
        if not data.get("fields"):
            continue
        name = (data.get("name") or path.stem).strip()
        if not name:
            continue
        index[name.lower()] = data
    return index


def _summary_entry(data: dict[str, Any]) -> dict[str, Any]:
    """Compact one-line entry for the listing mode."""
    supported = data.get("supported_os") or {}
    os_list = [k for k, v in supported.items() if v]
    return {
        "name": data.get("name"),
        "summary": data.get("summary"),
        "field_count": len(data.get("fields") or []),
        "example_count": len(data.get("examples") or []),
        "supported_os": os_list,
    }


def _resolve_field(data: dict[str, Any], field_name: str) -> Optional[dict[str, Any]]:
    target = field_name.strip().lower()
    for f in data.get("fields") or []:
        if str(f.get("name") or "").lower() == target:
            return f
    return None


class TrellixEdrCollectorsTool(BaseTool):
    """Reference lookup over the Trellix EDR built-in collectors.

    Three modes:

    * **list mode** — call with no arguments to get the catalog of every
      collector (name + summary + field/example count + supported OS).
    * **detail mode** — pass ``name="NetworkFlow"`` (case-insensitive) to get
      the full schema: every field, supported versions, v2 example queries
      and v1 Active Response payload skeletons.
    * **field mode** — pass ``name="NetworkFlow"`` and ``field="dst_ip"`` to
      get just one field's metadata (type, description, allowed enum values).

    Use this tool **before EVERY** ``trellix_edr_search`` call to confirm
     exact field names. Trellix sometimes accepts hallucinated field names
    silently and returns empty / partial rows — a query that looks like
    "success" but used a field that does not exist will mislead the
    investigation. Common pitfalls (do NOT guess from Sysinternals / WMI /
    osquery): Processes uses ``id`` (not ``pid``); Services uses
    ``description`` + ``startuptype`` (not ``displayname`` + ``starttype``);
    ScheduledTasks uses ``taskname`` + ``folder`` (not ``name`` + ``path``);
    UserProfiles uses ``localaccount`` (one word, no underscore).
    Otherwise the Trellix API rejects unknown outputs with ``AR-806``.

    v2 syntax reminders (see ``trellix_edr_search`` for the full guide):

    * Projections are mandatory: ``ScheduledTasks folder, taskname`` (never
      a bare ``ScheduledTasks``). Fields inside one collector are separated
      by COMMAS.
    * Different collectors are joined with the ``AND`` keyword (NOT a
      comma): ``Processes name, pid AND HostInfo hostname``.
    * String literals use **double quotes** (``equals "host01"``); numbers
      and IPs are **unquoted** (``equals 445``, ``equals 10.0.0.1``).
    * ``WHERE`` may reference any collector even if not projected — Trellix
      auto-joins by host. If the compact form returns HTTP 400 "Invalid
      value provided for query", use the explicit form projecting both
      collectors with ``AND`` (e.g. ``ScheduledTasks taskname, folder AND
      HostInfo hostname WHERE HostInfo hostname equals "host01"``).
    * Each collector entry includes ``v1_payload_example`` — use it as a
      template if v2 keeps failing and you need to fall back to the v1
      ``payload`` API.

    Permission: ``edr:read``
    """

    name: ClassVar[str] = "trellix_edr_collectors"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Reference catalogue of Trellix EDR built-in collectors, fields and "
        "example queries (purely local lookup, no API calls)"
    )
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["edr:read"]

    rate_limit_per_minute: ClassVar[int] = 120
    timeout_seconds: ClassVar[int] = 10
    use_circuit_breaker: ClassVar[bool] = False
    requires_approval: ClassVar[bool] = False
    always_background: ClassVar[bool] = False
    requires_config: ClassVar[bool] = False

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Collector name (case-insensitive, e.g. 'NetworkFlow', "
                    "'Autorun', 'DNSCache'). Omit to list every collector."
                ),
            },
            "field": {
                "type": "string",
                "description": (
                    "Optional field name within the chosen collector. "
                    "Requires 'name'. Returns just that field's metadata."
                ),
            },
            "include_examples": {
                "type": "boolean",
                "description": (
                    "Include v2 SQL-like example queries and v1 Active "
                    "Response payload skeletons in detail mode "
                    "(default: true)."
                ),
                "default": True,
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

        name = (params.get("name") or "").strip() or None
        field_name = (params.get("field") or "").strip() or None
        include_examples = bool(params.get("include_examples", True))

        if field_name and not name:
            return self._failure(
                "INVALID_INPUT",
                "Parameter 'field' requires 'name'.",
            )

        index = _load_index()

        # ── list mode ────────────────────────────────────────────────────
        if name is None:
            entries = sorted(
                (_summary_entry(d) for d in index.values()),
                key=lambda e: (e.get("name") or "").lower(),
            )
            return self._success(
                {
                    "mode": "list",
                    "total": len(entries),
                    "collectors": entries,
                },
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # ── detail / field mode ──────────────────────────────────────────
        data = index.get(name.lower())
        if data is None:
            available = sorted((d.get("name") or "") for d in index.values())
            return self._failure(
                "NOT_FOUND",
                (
                    f"Collector '{name}' is not in the catalogue. "
                    f"Available: {', '.join(available)}."
                ),
            )

        if field_name:
            field_data = _resolve_field(data, field_name)
            if field_data is None:
                available_fields = [
                    f.get("name") for f in data.get("fields") or []
                ]
                return self._failure(
                    "NOT_FOUND",
                    (
                        f"Field '{field_name}' is not defined for "
                        f"collector '{data.get('name')}'. Available: "
                        f"{', '.join(filter(None, available_fields))}."
                    ),
                )
            return self._success(
                {
                    "mode": "field",
                    "collector": data.get("name"),
                    "field": field_data,
                },
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        result: dict[str, Any] = {
            "mode": "detail",
            "name": data.get("name"),
            "summary": data.get("summary"),
            "supported_os": data.get("supported_os") or {},
            "fields": data.get("fields") or [],
        }
        if include_examples:
            result["examples"] = data.get("examples") or []
            v1 = data.get("v1_payload_example")
            if v1:
                result["v1_payload_example"] = v1

        return self._success(
            result,
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )
