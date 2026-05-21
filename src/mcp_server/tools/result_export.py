"""gSage AI — Generic tabular result export helpers for tools.

This module provides a **reusable pattern for tools that return tabular row
sets** (search results, inventory listings, paginated reports). It standardises
three orthogonal concerns so individual tools don't have to reinvent them:

1. **Serialisation** — :func:`export_to_csv` / :func:`export_to_json`.
2. **Artifact persistence** — :func:`maybe_export_artifacts` uploads the
   serialised bytes via :meth:`BaseTool._store_file` (MinIO + DB row), reusing
   the in-flight ``AsyncSession`` from :data:`_tool_session_ctx` when
   available.
3. **Agent-facing payload shaping** — :func:`build_agent_payload` caps the
   inline ``rows`` shipped to the LLM (default 100) and **automatically forces
   CSV generation when the full result set overflows the cap**, regardless of
   whether the caller opted into ``export_csv``. The user always gets a
   downloadable file; the agent gets a small, focused preview plus an
   ``agent_hint`` instructing it to surface the download link instead of
   trying to enumerate thousands of rows in chat.

Why a shared helper
-------------------
This pattern complements the other tool-framework primitives:

- **Background execution** (``always_background = True``) — long-running tools
  that poll an upstream API. Background tools store their final ``ToolResult``
  in the DB and notify the channel when ready.
- **Tool config namespaces** — multiple instances of the same tool with
  different credentials per org/dept (see ``44-TOOL-CONFIG-NAMESPACE``).
- **Approval / HITL** — destructive actions surface a preview before
  executing.
- **Audit cache / output capture** — every ``ToolResult`` is persisted for
  forensic replay.

Result export sits alongside those primitives: it is the canonical answer to
"my tool can return 5000 rows and the agent context can't hold them all".

Adopting the pattern
--------------------
A typical tool ``execute`` flow becomes::

    from src.mcp_server.tools.result_export import (
        AGENT_PREVIEW_ROWS,
        build_agent_payload,
        summarize,
    )

    # 1. Run the upstream query, build a flat list[dict] of rows.
    rows = normalize_and_enrich(raw_items)

    # 2. Build the (optional) top-N analytical summary.
    summary = summarize(rows, group_by=params.get("group_by"),
                        top_n=int(params.get("top_n", 10)))

    # 3. Build the agent payload (handles preview cap + CSV overflow).
    agent_payload = await build_agent_payload(
        tool=self,
        rows=rows,
        export_csv=bool(params.get("export_csv", False)),
        export_json=bool(params.get("export_json", False)),
        filename_prefix=f"{self.name}_{action}",
        agent_context=agent_context,
    )

    return self._success({
        "action": action,
        "rows_total": agent_payload["rows_total"],
        "rows_overflow": agent_payload["rows_overflow"],
        "rows_preview_limit": AGENT_PREVIEW_ROWS,
        "artifacts": agent_payload["artifacts"],
        "agent_hint": agent_payload["agent_hint"],
        "summary": summary,
        "rows": agent_payload["rows_preview"],
    })

The corresponding ``params_schema`` entries are::

    "export_csv": {"type": "boolean", "default": False, ...},
    "export_json": {"type": "boolean", "default": False, ...},
    "group_by":   {"type": "array", "items": {"type": "string"}, ...},
    "top_n":      {"type": "integer", "minimum": 1, "maximum": 50,
                    "default": 10, ...},

Enrichment is a *tool* responsibility — perform any normalisation, joining,
reverse-coding (e.g., int enum → human-readable string) or geo/whois lookups
**before** calling :func:`build_agent_payload`, so both the inline preview
and the persisted CSV reflect the enriched view.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import tempfile
import time
from collections import Counter
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterable, Optional

if TYPE_CHECKING:
    from src.mcp_server.tools.base import BaseTool, ToolResult
    from src.shared.security.context import AgentContext

# A streaming row source: an async iterator yielding ``(row, server_total)``
# tuples one row at a time. ``server_total`` is the upstream-reported total
# row count (may be None when the API does not expose it). Generic across
# vendors — the egoi/openvas/trellix adapters wrap their paginators in this
# shape so :func:`run_streaming_export` can persist the rows without ever
# materialising the full list in memory.
RowStreamer = AsyncIterator[tuple[dict, Optional[int]]]

log = logging.getLogger(__name__)

# Maximum number of rows embedded in the tool result returned to the agent.
# Larger result sets are still searched and (always) exported to CSV in full,
# but only the first AGENT_PREVIEW_ROWS rows are shipped inline so the agent's
# context is not flooded with thousands of records.
AGENT_PREVIEW_ROWS: int = 100


# ── Serialisation ───────────────────────────────────────────────────────────

def export_to_csv(rows: list[dict]) -> bytes:
    """Encode rows as UTF-8 CSV.

    Columns are the union of all keys across rows, preserving first-seen
    order. Non-scalar values are JSON-encoded so the CSV stays single-row
    per record.
    """
    if not rows:
        return b""
    columns: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                columns.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: _csv_value(r.get(k)) for k in columns})
    return buf.getvalue().encode("utf-8")


def _csv_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    try:
        return json.dumps(v, default=str, ensure_ascii=False)
    except Exception:
        return str(v)


def export_to_json(rows: list[dict]) -> bytes:
    """Encode rows as a UTF-8 JSON array."""
    return json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")


# ── Top-N summarisation ─────────────────────────────────────────────────────

def summarize(
    rows: list[dict],
    *,
    group_by: Optional[Iterable[str]] = None,
    top_n: int = 10,
    sample_size: int = 20,
    default_keys: Optional[Iterable[str]] = None,
    max_default_keys: int = 8,
) -> dict:
    """Build a generic top-N + distinct-counts summary over flat rows.

    Parameters
    ----------
    rows:
        Flat ``list[dict]`` (all values must be hashable or JSON-serialisable).
    group_by:
        Explicit list of column names to summarise. When given, ``default_keys``
        is ignored.
    top_n:
        Maximum number of (value, count) pairs per column.
    sample_size:
        How many of the original rows to echo back in ``sample`` (helpful for
        the agent to peek at row shape).
    default_keys:
        Tool-specific priority list of columns to use when ``group_by`` is not
        provided. Only keys that are actually present in at least one row are
        kept; the first ``max_default_keys`` survive.
    """
    if not rows:
        return {"row_count": 0, "distinct": {}, "top": {}, "sample": []}

    keys: list[str]
    if group_by:
        keys = [str(k) for k in group_by if k]
    elif default_keys:
        present = set().union(*(r.keys() for r in rows))
        keys = [k for k in default_keys if k in present][:max_default_keys]
    else:
        keys = []

    distinct: dict[str, int] = {}
    top: dict[str, list[dict]] = {}
    for k in keys:
        values = [r.get(k) for r in rows if r.get(k) not in (None, "")]
        distinct[k] = len({_hashable(v) for v in values})
        counter: Counter[Any] = Counter(_hashable(v) for v in values)
        top[k] = [
            {"value": val, "count": cnt}
            for val, cnt in counter.most_common(top_n)
        ]

    return {
        "row_count": len(rows),
        "distinct": distinct,
        "top": top,
        "sample": rows[:sample_size],
    }


def _hashable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return str(value)


# ── Artifact persistence + agent payload ────────────────────────────────────

async def maybe_export_artifacts(
    tool: "BaseTool",
    *,
    rows: list[dict],
    export_csv: bool,
    export_json: bool,
    filename_prefix: str,
    agent_context: "AgentContext",
) -> dict:
    """Optionally persist rows as CSV/JSON file artifacts.

    Returns a dict with ``csv_file`` / ``json_file`` keys; each is a file-info
    dict produced by :meth:`BaseTool._store_file` (``file_id``, ``filename``,
    ``content_type``, ``size_bytes``, ``download_path``, ``expires_at``) or
    ``None`` when not requested / when the upload failed.

    Failures are logged but never raised — the caller still gets a usable
    payload (with the inline preview) and can warn the user separately.
    """
    artifacts: dict = {
        "csv_file": None,
        "json_file": None,
        "csv_error": None,
        "json_error": None,
    }
    if not rows or (not export_csv and not export_json):
        return artifacts

    ts = int(time.time())
    safe_prefix = _safe_filename_prefix(filename_prefix)

    if export_csv:
        try:
            stored = await store_export_artifact(
                tool=tool,
                agent_context=agent_context,
                data=export_to_csv(rows),
                filename=f"{safe_prefix}_{ts}.csv",
                content_type="text/csv",
            )
            artifacts["csv_file"] = stored
            if stored is None:
                # _store_file already logged the underlying cause; surface
                # a generic marker so the agent knows the export was
                # attempted but did not persist (e.g. MinIO offline, size
                # cap exceeded, DB commit refused).
                artifacts["csv_error"] = (
                    "file storage returned no record (see backend logs "
                    "for the underlying cause)"
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: CSV export failed: %s", tool.name, exc)
            artifacts["csv_error"] = f"{type(exc).__name__}: {exc}"

    if export_json:
        try:
            stored = await store_export_artifact(
                tool=tool,
                agent_context=agent_context,
                data=export_to_json(rows),
                filename=f"{safe_prefix}_{ts}.json",
                content_type="application/json",
            )
            artifacts["json_file"] = stored
            if stored is None:
                artifacts["json_error"] = (
                    "file storage returned no record (see backend logs "
                    "for the underlying cause)"
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: JSON export failed: %s", tool.name, exc)
            artifacts["json_error"] = f"{type(exc).__name__}: {exc}"

    return artifacts


async def build_agent_payload(
    tool: "BaseTool",
    *,
    rows: list[dict],
    export_csv: bool,
    export_json: bool,
    filename_prefix: str,
    agent_context: "AgentContext",
    preview_rows: int = AGENT_PREVIEW_ROWS,
) -> dict:
    """Prepare the agent-facing payload for a tabular tool result.

    Behaviour
    ---------
    - Caps inline ``rows`` at ``preview_rows`` (default 100).
    - When the full result set exceeds the cap, **forces CSV generation**
      regardless of the ``export_csv`` flag, so the user always has a
      downloadable file with the complete data.
    - Builds an ``agent_hint`` string for the LLM, instructing it to present
      the download link instead of enumerating every row in chat.

    Returns
    -------
    dict
        ``{"artifacts": {...}, "rows_preview": [...], "rows_total": int,
        "rows_overflow": bool, "agent_hint": str | None}``
    """
    rows_total = len(rows)
    rows_overflow = rows_total > preview_rows
    effective_export_csv = export_csv or rows_overflow

    artifacts = await maybe_export_artifacts(
        tool,
        rows=rows,
        export_csv=effective_export_csv,
        export_json=export_json,
        filename_prefix=filename_prefix,
        agent_context=agent_context,
    )

    rows_preview = rows[:preview_rows] if rows_overflow else rows

    agent_hint: Optional[str] = None
    if rows_overflow:
        csv_info = artifacts.get("csv_file") or {}
        download_path = csv_info.get("download_path")
        file_id = csv_info.get("file_id")
        agent_hint = (
            f"Result has {rows_total} rows; only the first "
            f"{preview_rows} are inlined in 'rows'. The full result "
            "has been saved as a CSV artifact — present the download link "
            "to the user instead of trying to enumerate every row in chat."
        )
        if download_path:
            agent_hint += f" download_path={download_path}"
        elif file_id:
            agent_hint += f" file_id={file_id}"

    return {
        "artifacts": artifacts,
        "rows_preview": rows_preview,
        "rows_total": rows_total,
        "rows_overflow": rows_overflow,
        "agent_hint": agent_hint,
    }


# ── Streaming helpers (memory-bounded export for huge result sets) ─────────


def _safe_filename_prefix(prefix: str) -> str:
    """Sanitise a user-supplied prefix into a filesystem-safe stem."""
    return "".join(
        c if c.isalnum() or c in "._-" else "_" for c in prefix
    )[:80]


async def store_export_artifact(
    *,
    tool: "BaseTool",
    agent_context: "AgentContext",
    data: bytes,
    filename: str,
    content_type: str,
) -> Optional[dict]:
    """Persist *data* via :meth:`BaseTool._store_file` reusing the in-flight
    tool-execution session when available.

    Falls back to a fresh :class:`AsyncSession` when the tool runs outside
    the ``BaseTool.run`` orchestration (e.g. ad-hoc scripts). Returns the
    file-info dict produced by ``_store_file`` (``file_id``, ``filename``,
    ``content_type``, ``size_bytes``, ``download_path``, ``expires_at``)
    or ``None`` on storage failure.
    """
    from src.mcp_server.tools.base import _tool_session_ctx  # noqa: PLC0415

    ctx_session = _tool_session_ctx.get()
    if ctx_session is not None:
        return await tool._store_file(  # type: ignore[attr-defined]
            data=data,
            filename=filename,
            content_type=content_type,
            agent_context=agent_context,
            session=ctx_session,
            description=f"{tool.name} export",
        )
    from src.shared.database import _get_session_maker  # noqa: PLC0415

    async with _get_session_maker()() as db_session:
        return await tool._store_file(  # type: ignore[attr-defined]
            data=data,
            filename=filename,
            content_type=content_type,
            agent_context=agent_context,
            session=db_session,
            description=f"{tool.name} export",
        )


class IncrementalSummary:
    """Streaming-friendly counterpart of :func:`summarize`.

    Mirrors the output shape (``row_count`` / ``distinct`` / ``top`` /
    ``sample``) but accumulates statistics one row at a time, so the
    full row list never needs to be held in memory. Designed for tools
    that paginate huge result sets and persist them straight to disk.
    """

    def __init__(
        self,
        *,
        group_by: Optional[Iterable[str]] = None,
        top_n: int = 10,
        sample_size: int = 20,
    ) -> None:
        self._keys = [str(k) for k in (group_by or []) if k]
        self._top_n = max(1, int(top_n))
        self._sample_size = max(0, int(sample_size))
        self._counters: dict[str, Counter] = {k: Counter() for k in self._keys}
        self._sample: list[dict] = []
        self._row_count = 0

    def add(self, row: dict) -> None:
        self._row_count += 1
        if len(self._sample) < self._sample_size:
            self._sample.append(row)
        for k in self._keys:
            v = row.get(k)
            if v in (None, ""):
                continue
            self._counters[k][_hashable(v)] += 1

    def finalize(self) -> dict:
        if self._row_count == 0:
            return {"row_count": 0, "distinct": {}, "top": {}, "sample": []}
        distinct = {k: len(self._counters[k]) for k in self._keys}
        top = {
            k: [
                {"value": val, "count": cnt}
                for val, cnt in self._counters[k].most_common(self._top_n)
            ]
            for k in self._keys
        }
        return {
            "row_count": self._row_count,
            "distinct": distinct,
            "top": top,
            "sample": self._sample,
        }


async def run_streaming_export(
    tool: "BaseTool",
    *,
    agent_context: "AgentContext",
    streamer: RowStreamer,
    filename_prefix: str,
    csv_columns: list[str],
    preview_rows: int = AGENT_PREVIEW_ROWS,
    summary_group_by: Optional[list[str]] = None,
    summary_top_n: int = 10,
    summary_sample_size: int = 20,
    extra_data: Optional[dict] = None,
    operation_label: str = "streaming export",
) -> "ToolResult":
    """Persist a paginated row stream as a CSV artifact with constant memory.

    Designed for vendor-agnostic, very large enumerations (tens to hundreds
    of thousands of rows) that would otherwise OOM the worker. Behaviour:

    * Iterates the caller-provided async generator, writing each row
      directly to a CSV file inside a per-execution
      :class:`tempfile.TemporaryDirectory`.
    * Keeps only the first ``preview_rows`` rows in memory (for the agent
      preview) plus an :class:`IncrementalSummary` (Counters + small sample).
    * After the stream completes, reads the temp CSV back as bytes and
      uploads it via :func:`store_export_artifact` (single short-lived
      buffer); the temp directory is removed automatically.

    The CSV column order is fixed by ``csv_columns`` (callers should pass
    the canonical schema of their normaliser); any extra keys produced
    later are silently dropped from the file (they still appear in the
    in-memory preview rows). Vendors typically wrap this helper in a thin
    runner that opens their SDK client and translates vendor-specific
    errors into ``tool._failure(...)`` codes.

    Returns a ``ToolResult`` ready to be returned from ``execute``.
    """
    t0 = time.monotonic()
    preview: list[dict] = []
    incsum = IncrementalSummary(
        group_by=summary_group_by,
        top_n=summary_top_n,
        sample_size=summary_sample_size,
    )
    server_total: Optional[int] = None
    rows_fetched = 0

    safe_prefix = _safe_filename_prefix(filename_prefix)
    ts = int(time.time())
    csv_filename = f"{safe_prefix}_{ts}.csv"
    artifacts: dict[str, Any] = {
        "csv_file": None,
        "json_file": None,
        "csv_error": None,
        "json_error": None,
    }

    try:
        with tempfile.TemporaryDirectory(prefix="gsage-export-") as tmpdir:
            tmp_path = os.path.join(tmpdir, csv_filename)
            # newline="" is the documented requirement for csv.DictWriter
            # to avoid spurious blank lines on Windows; harmless on Linux.
            with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=csv_columns, extrasaction="ignore"
                )
                writer.writeheader()
                async for row, total in streamer:
                    if server_total is None and isinstance(total, int):
                        server_total = total
                    rows_fetched += 1
                    if len(preview) < preview_rows:
                        preview.append(row)
                    incsum.add(row)
                    writer.writerow(
                        {k: _csv_value(row.get(k)) for k in csv_columns}
                    )

            csv_size = os.path.getsize(tmp_path)
            if rows_fetched > 0:
                with open(tmp_path, "rb") as fh:
                    csv_bytes = fh.read()
                stored = await store_export_artifact(
                    tool=tool,
                    agent_context=agent_context,
                    data=csv_bytes,
                    filename=csv_filename,
                    content_type="text/csv",
                )
                artifacts["csv_file"] = stored
                if stored is None:
                    artifacts["csv_error"] = (
                        "file storage returned no record (see backend logs "
                        "for the underlying cause)"
                    )
            log.info(
                "%s: streamed %d rows (server_total=%s, csv=%d bytes)",
                operation_label, rows_fetched, server_total, csv_size,
            )
    except Exception as exc:  # noqa: BLE001
        # Vendor-specific error translation is the caller's job (a thin
        # wrapper around this helper). Anything that escapes the streamer
        # is logged here and surfaced as INTERNAL_ERROR so the worker
        # never dies silently.
        log.exception("%s: unexpected error during streaming export", operation_label)
        elapsed = int((time.monotonic() - t0) * 1000)
        return tool._failure(  # type: ignore[attr-defined]
            "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
        )

    overflow = rows_fetched > preview_rows
    if isinstance(server_total, int) and server_total > rows_fetched:
        overflow = True

    csv_info = artifacts.get("csv_file") or {}
    download_path = csv_info.get("download_path") if isinstance(csv_info, dict) else None
    file_id = csv_info.get("file_id") if isinstance(csv_info, dict) else None
    agent_hint: Optional[str] = None
    if overflow or rows_fetched > preview_rows:
        agent_hint = (
            f"Result has {rows_fetched} rows; only the first "
            f"{preview_rows} are inlined in 'rows'. The full result has "
            "been saved as a CSV artifact — present the download link "
            "to the user instead of trying to enumerate every row in chat."
        )
        if download_path:
            agent_hint += f" download_path={download_path}"
        elif file_id:
            agent_hint += f" file_id={file_id}"

    elapsed = int((time.monotonic() - t0) * 1000)
    payload: dict[str, Any] = {
        "rows_total": (
            int(server_total) if isinstance(server_total, int) else rows_fetched
        ),
        "rows_fetched": rows_fetched,
        "server_total_items": (
            int(server_total) if isinstance(server_total, int) else None
        ),
        "rows_overflow": overflow,
        "rows": preview,
        "summary": incsum.finalize(),
        "artifacts": artifacts,
        "agent_hint": agent_hint,
    }
    if extra_data:
        payload.update(extra_data)
    return tool._success(payload, execution_time_ms=elapsed)  # type: ignore[attr-defined]
