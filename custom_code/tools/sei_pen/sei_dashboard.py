"""gSage AI — SEI-PEN managerial dashboard tool.

Read-only aggregations over the caller's SEI processes/documents. Each view
returns ``{summary, rows, mermaid, truncated}`` (Mermaid optional). Mirrors the
``glpi_dashboard`` pattern: thin tool + ``_dashboard`` view module.

Shares configuration and credentials with the other ``sei_pen_*`` tools
(``config_namespace`` / ``credential_namespace`` = ``sei_pen``).

Required permission: ``sei:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

from custom_code.tools.sei_pen import _dashboard as views
from custom_code.tools.sei_pen._client import (
    SeiPenClient,
    SeiPenError,
    resolve_base_url,
    with_hint,
)

log = logging.getLogger(__name__)

_VIEWS = (
    "meus_processos",
    "prazos",
    "documentos_por_processo",
    "processos_por_tipo",
    "processos_por_assunto",
    "acompanhamentos",
)


class SeiPenDashboardTool(BaseTool):
    """Compute managerial views over the caller's SEI processes.

    Pick a ``view``:

    - ``meus_processos``: open processes assigned to the caller (counts by
      situation + table; pie by situation).
    - ``prazos``: idle time per process ("tempo parado na caixa") — days since
      the last activity, bucketed (< 7d / 7–30d / > 30d); pie by bucket.
    - ``documentos_por_processo`` (requires ``procedimento``): documents of a
      process grouped by type (série); pie.
    - ``processos_por_tipo``: distribution across process types; pie.
    - ``processos_por_assunto``: distribution across subjects (grouped by the
      process specification, as the listing has no structured subject); pie.
    - ``acompanhamentos``: tracked processes grouped by tracking group (may be
      unavailable on installations affected by the known WSSEI server bug).

    Permission: ``sei:read``
    """

    name: ClassVar[str] = "sei_pen_dashboard"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Managerial SEI dashboards: my processes, deadlines (idle time), "
        "documents per process, distribution by type/subject, tracking groups"
    )
    category: ClassVar[str] = "document"
    permissions: ClassVar[list[str]] = ["sei:read"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 90
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = False

    # Shared org-level configuration and user keychain entry across all
    # ``sei_pen_*`` tools.
    config_namespace: ClassVar[Optional[str]] = "sei_pen"
    requires_user_credentials: ClassVar[bool] = True
    credential_namespace: ClassVar[Optional[str]] = "sei_pen"
    credential_schema: ClassVar[Optional[dict]] = {
        "kind": "basic",
        "required": ["username", "password"],
        "optional": [],
        "extra_fields": {
            "required": ["orgao_id"],
            "optional": ["unidade_id"],
            "properties": {
                "orgao_id": (
                    "SEI organ/agency numeric ID (e.g. '0' for the default organ)."
                ),
                "unidade_id": (
                    "Default unit ID sent with every request to maintain "
                    "session context."
                ),
            },
        },
        "description": (
            "SEI-PEN username and password (login/sigla and senha). "
            "Add 'orgao_id' (required) and 'unidade_id' (optional) as extra "
            "fields of this credential."
        ),
    }

    audit_field_mapping: ClassVar[dict] = {}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["view"],
        "properties": {
            "view": {
                "type": "string",
                "enum": sorted(_VIEWS),
                "description": "Which managerial aggregation to compute.",
            },
            "procedimento": {
                "type": "string",
                "description": (
                    "Process internal ID. Required for: documentos_por_processo."
                ),
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Row cap for leaderboard rows (used by 'prazos').",
            },
            "unidade": {
                "type": "string",
                "description": (
                    "Unit ID to override the default session unit context for "
                    "this request."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "ambiente": {
                "type": "string",
                "enum": [
                    "producao_orgao1", "producao_orgao2", "producao_orgao3",
                    "producao_orgao4", "producao_orgao5",
                    "homologacao_orgao1", "homologacao_orgao2", "homologacao_orgao3",
                    "homologacao_orgao4", "homologacao_orgao5",
                ],
                "description": (
                    "SEI-PEN environment preset. Maps to the standard WSSEI v2 URL. "
                    "Use 'base_url' for non-standard installations."
                ),
            },
            "base_url": {
                "type": "string",
                "description": (
                    "Custom WSSEI v2 base URL. Overrides 'ambiente' when set."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    }
    config_defaults: ClassVar[dict] = {}

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.perf_counter()
        view: str = params["view"]
        if view not in _VIEWS:
            return self._failure(
                "INVALID_PARAMS", f"view must be one of {sorted(_VIEWS)}; got {view!r}."
            )

        # ── Resolve base URL ──────────────────────────────────────────────────
        try:
            base_url = resolve_base_url(config.get("ambiente"), config.get("base_url"))
        except SeiPenError as exc:
            return self._failure("CONFIG_ERROR", str(exc))

        # ── Resolve credentials (user keychain shared by all sei_pen_* tools) ──
        user_creds = agent_context.user_credentials.get("sei_pen") or {}
        usuario: str = (user_creds.get("username") or "").strip()
        senha: str = (user_creds.get("password") or "").strip()
        extra: dict = user_creds.get("extra_fields") or {}
        orgao_id: str = str(extra.get("orgao_id", "") or "").strip()
        unidade_id: Optional[str] = str(extra.get("unidade_id", "") or "").strip() or None
        if not usuario or not senha:
            return self._failure(
                "CREDENTIAL_MISSING",
                "SEI-PEN requires a personal credential. Configure your "
                "'sei_pen' credential in Settings → Credentials and link "
                "it as active for this tool.",
            )
        if not orgao_id:
            return self._failure(
                "CREDENTIAL_MISSING",
                "SEI-PEN credential must include an 'orgao_id' extra field. "
                "Edit your 'sei_pen' credential in Settings → Credentials and "
                "add 'orgao_id' (and optionally 'unidade_id') as extra fields.",
            )

        client = SeiPenClient(
            base_url=base_url,
            usuario=usuario,
            senha=senha,
            orgao_id=orgao_id,
            unidade_id=unidade_id,
            timeout=float(self.timeout_seconds),
        )
        unidade_override: Optional[str] = params.get("unidade")

        try:
            if view == "meus_processos":
                data = await views.meus_processos(
                    client, unidade_override=unidade_override
                )
            elif view == "prazos":
                data = await views.prazos(
                    client,
                    unidade_override=unidade_override,
                    top_n=int(params.get("top_n") or 20),
                )
            elif view == "processos_por_tipo":
                data = await views.processos_por_tipo(
                    client, unidade_override=unidade_override
                )
            elif view == "processos_por_assunto":
                data = await views.processos_por_assunto(
                    client, unidade_override=unidade_override
                )
            elif view == "documentos_por_processo":
                procedimento = params.get("procedimento")
                if not procedimento:
                    return self._failure(
                        "INVALID_PARAMS",
                        "documentos_por_processo requires 'procedimento'.",
                    )
                data = await views.documentos_por_processo(
                    client,
                    procedimento=str(procedimento),
                    unidade_override=unidade_override,
                )
            elif view == "acompanhamentos":
                data = await views.acompanhamentos(
                    client, unidade_override=unidade_override
                )
            else:  # pragma: no cover — guarded by enum
                return self._failure("INVALID_PARAMS", f"unknown view {view!r}")
        except SeiPenError as exc:
            log.warning(
                "sei_pen_dashboard: view=%s error=%s", view, exc, exc_info=True
            )
            retryable = (
                exc.status_code in (429, 500, 502, 503, 504)
                if exc.status_code
                else True
            )
            return self._failure(
                "SEI_API_ERROR",
                with_hint(str(exc), exc.status_code, view),
                retryable=retryable,
                execution_time_ms=round((time.perf_counter() - t0) * 1000),
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        log.info(
            "sei_pen_dashboard: view=%s status=success elapsed_ms=%d", view, elapsed_ms
        )
        return self._success(
            {"view": view, "data": data},
            execution_time_ms=elapsed_ms,
        )
