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
