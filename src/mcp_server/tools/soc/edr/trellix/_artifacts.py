"""gSage AI — Trellix EDR shared tool helpers.

Glue between the pure :mod:`_query` module and :class:`BaseTool` —
artifact uploads, audit-friendly summary post-processing.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from src.mcp_server.tools.base import BaseTool, _tool_session_ctx
from src.mcp_server.tools.soc.edr.trellix import _query as Q
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


async def maybe_export(
    tool: BaseTool,
    *,
    rows: list[dict],
    export_csv: bool,
    export_json: bool,
    filename_prefix: str,
    agent_context: AgentContext,
) -> dict:
    """Optionally upload rows as CSV/JSON and return artifact info.

    Returns a dict with ``csv_file`` / ``json_file`` keys (each a file-info
    dict from :meth:`BaseTool._store_file` or ``None`` on failure / when
    not requested).
    """
    artifacts: dict = {"csv_file": None, "json_file": None}
    if not rows or (not export_csv and not export_json):
        return artifacts

    ts = int(time.time())
    safe_prefix = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename_prefix)[:80]

    async def _store(data: bytes, filename: str, content_type: str) -> Optional[dict]:
        ctx_session = _tool_session_ctx.get()
        if ctx_session is not None:
            return await tool._store_file(  # pyright: ignore[reportPrivateUsage]
                data=data,
                filename=filename,
                content_type=content_type,
                agent_context=agent_context,
                session=ctx_session,
                description=f"{tool.name} export",
            )
        from src.shared.database import _get_session_maker  # noqa: PLC0415

        async with _get_session_maker()() as db_session:
            return await tool._store_file(  # pyright: ignore[reportPrivateUsage]
                data=data,
                filename=filename,
                content_type=content_type,
                agent_context=agent_context,
                session=db_session,
                description=f"{tool.name} export",
            )

    if export_csv:
        try:
            artifacts["csv_file"] = await _store(
                Q.export_to_csv(rows),
                f"{safe_prefix}_{ts}.csv",
                "text/csv",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("trellix_edr: CSV export failed: %s", exc)

    if export_json:
        try:
            artifacts["json_file"] = await _store(
                Q.export_to_json(rows),
                f"{safe_prefix}_{ts}.json",
                "application/json",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("trellix_edr: JSON export failed: %s", exc)

    return artifacts


async def build_agent_payload(
    tool: BaseTool,
    *,
    rows: list[dict],
    export_csv: bool,
    export_json: bool,
    filename_prefix: str,
    agent_context: AgentContext,
) -> dict:
    """Prepare the per-tool agent-facing payload for a Trellix search.

    Caps the inline ``rows`` shipped to the agent at
    :data:`Q.AGENT_PREVIEW_ROWS` (100). When the full result set exceeds that
    cap, a CSV artifact is **always** generated (regardless of the
    ``export_csv`` parameter) so the agent can hand the user a downloadable
    file with the complete data. Returns a dict with stable keys ready to be
    merged into ``result_data``::

        {
            "artifacts": {"csv_file": {...} | None, "json_file": {...} | None},
            "rows_preview": [...],   # first 100 rows
            "rows_total": int,        # full row count before truncation
            "rows_overflow": bool,    # True if rows_total > 100
        }
    """
    rows_total = len(rows)
    rows_overflow = rows_total > Q.AGENT_PREVIEW_ROWS

    # When the full result set exceeds the agent preview cap we force CSV
    # generation so the user always has a way to download the complete data.
    effective_export_csv = export_csv or rows_overflow

    artifacts = await maybe_export(
        tool,
        rows=rows,
        export_csv=effective_export_csv,
        export_json=export_json,
        filename_prefix=filename_prefix,
        agent_context=agent_context,
    )

    rows_preview = rows[: Q.AGENT_PREVIEW_ROWS] if rows_overflow else rows

    agent_hint: Optional[str] = None
    if rows_overflow:
        csv_info = artifacts.get("csv_file") or {}
        download_path = csv_info.get("download_path")
        file_id = csv_info.get("file_id")
        agent_hint = (
            f"Result has {rows_total} rows; only the first "
            f"{Q.AGENT_PREVIEW_ROWS} are inlined in 'rows'. The full result "
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
