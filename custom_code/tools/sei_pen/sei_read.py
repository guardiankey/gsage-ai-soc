"""gSage AI — SEI-PEN Read tool.

Queries the SEI (Sistema Eletrônico de Informações) WSSEI REST API v2 for
read-only information: organs, processes, documents, units, document models,
tracking groups, special tracking, and users.

Required permission: ``sei:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

from custom_code.tools.sei_pen._client import SeiPenClient, SeiPenError, resolve_base_url
from custom_code.tools.sei_pen._operations import BuildError, READ_OPERATIONS, build_request

log = logging.getLogger(__name__)

# Sorted list of all read operation IDs for the params schema enum
_READ_OP_IDS = sorted(READ_OPERATIONS.keys())


class SeiPenReadTool(BaseTool):
    """Query the SEI WSSEI REST API v2 for read-only information.

    **Available operations**

    | Operation                              | Description                                           |
    |----------------------------------------|-------------------------------------------------------|
    | ``orgao.listar``                       | List all organs in the installation                   |
    | ``documento.visualizar``               | Retrieve HTML content of an internal document         |
    | ``documento.consultar_interno``        | Retrieve metadata of an internal document             |
    | ``documento.listar_em_processo``       | List documents attached to a process                  |
    | ``unidade.pesquisar``                  | Search organizational units                           |
    | ``unidade.pesquisar_outras``           | Search units from other organs                        |
    | ``unidade.pesquisar_texto_padrao``     | Search internal standard texts for a unit             |
    | ``processo.pesquisar_assunto``         | Search process subjects                               |
    | ``processo.listar_meus_acompanhamentos`` | List current user's tracked processes               |
    | ``processo.listar_acompanhamentos``    | List tracked processes for a group                    |
    | ``processo.pesquisar_geral``           | Full-text search across processes                     |
    | ``processo.listar``                    | List processes with optional filters                  |
    | ``processo.consultar``                 | Retrieve full details of a process by protocol        |
    | ``processo.consultar_atribuicao``      | Retrieve assignment info of a process                 |
    | ``processo.consultar_acompanhamento``  | Retrieve tracking details for a process               |
    | ``grupo_acompanhamento.listar``        | List tracking groups                                  |
    | ``modelo_documento.listar_grupo``      | List document model groups                            |
    | ``modelo_documento.listar``            | List document models                                  |
    | ``acompanhamento_especial.listar``     | List special tracking entries                         |
    | ``usuario.pesquisar``                  | Search users by keyword                               |
    | ``usuario.listar_unidades``            | List units accessible to a user                       |

    Permission: ``sei:read``
    """

    name: ClassVar[str] = "sei_pen_read"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Read processes, documents, units, and metadata from the SEI "
        "government document management system via the WSSEI REST API v2"
    )
    category: ClassVar[str] = "document"
    permissions: ClassVar[list[str]] = ["sei:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {}

    # ── Tool params ───────────────────────────────────────────────────────────

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": _READ_OP_IDS,
                "description": (
                    "SEI-PEN read operation to perform. "
                    "See tool description for the complete list and required params per operation."
                ),
            },
            # ── Pagination (multiple operations) ─────────────────────────────
            "limit": {
                "type": "integer",
                "description": "Max records per page. Optional for paginated operations.",
            },
            "start": {
                "type": "integer",
                "description": "Page start offset (0-based). Optional for paginated operations.",
            },
            "filter": {
                "type": "string",
                "description": (
                    "Keyword filter. Optional for: unidade.pesquisar, "
                    "unidade.pesquisar_outras, unidade.pesquisar_texto_padrao, "
                    "processo.pesquisar_assunto, grupo_acompanhamento.listar, "
                    "modelo_documento.listar_grupo, modelo_documento.listar, processo.listar."
                ),
            },
            "id": {
                "type": "string",
                "description": (
                    "ID for detail lookup. Optional for: unidade.pesquisar_outras, "
                    "unidade.pesquisar_texto_padrao, processo.pesquisar_assunto, "
                    "grupo_acompanhamento.listar, modelo_documento.listar_grupo, "
                    "modelo_documento.listar, processo.listar."
                ),
            },
            # ── Document params ───────────────────────────────────────────────
            "documento": {
                "type": "string",
                "description": (
                    "Internal document ID (numeric). "
                    "Required for: documento.visualizar."
                ),
            },
            "protocolo": {
                "type": "string",
                "description": (
                    "Document or process protocol / internal ID. "
                    "Required for: documento.consultar_interno, processo.consultar, "
                    "processo.consultar_atribuicao, processo.consultar_acompanhamento."
                ),
            },
            "procedimento": {
                "type": "string",
                "description": (
                    "Process internal ID (numeric). "
                    "Required for: documento.listar_em_processo."
                ),
            },
            # ── Process / tracking params ─────────────────────────────────────
            "grupo": {
                "type": "string",
                "description": (
                    "Tracking group ID. "
                    "Required for: processo.listar_acompanhamentos. "
                    "Optional for: processo.listar_meus_acompanhamentos, processo.pesquisar_geral."
                ),
            },
            "usuario": {
                "type": "string",
                "description": (
                    "User login (sigla). "
                    "Required for: usuario.listar_unidades. "
                    "Optional for: processo.listar_meus_acompanhamentos, processo.listar."
                ),
            },
            "palavrachave": {
                "type": "string",
                "description": (
                    "Search keyword. Required for: usuario.pesquisar."
                ),
            },
            "orgao": {
                "type": "string",
                "description": "Organ ID filter. Optional for: usuario.pesquisar.",
            },
            # ── Full-text process search ──────────────────────────────────────
            "palavrasChave": {
                "type": "string",
                "description": "Keywords for full-text process search. Optional for: processo.pesquisar_geral.",
            },
            "descricao": {
                "type": "string",
                "description": "Description filter. Optional for: processo.pesquisar_geral.",
            },
            "staTipoData": {
                "type": "string",
                "description": "Date type flag. Optional for: processo.pesquisar_geral.",
            },
            "dataInicio": {
                "type": "string",
                "description": "Start date (dd/MM/yyyy). Optional for: processo.pesquisar_geral.",
            },
            "dataFim": {
                "type": "string",
                "description": "End date (dd/MM/yyyy). Optional for: processo.pesquisar_geral.",
            },
            "idUnidadeGeradora": {
                "type": "string",
                "description": "Generating unit ID. Optional for: processo.pesquisar_geral.",
            },
            "idAssunto": {
                "type": "string",
                "description": "Subject ID. Optional for: processo.pesquisar_geral.",
            },
            "buscaRapida": {
                "type": "string",
                "description": "Quick-search term. Optional for: processo.pesquisar_geral.",
            },
            # ── Process list filters ──────────────────────────────────────────
            "tipo": {
                "type": "string",
                "description": "Process type filter. Optional for: processo.listar.",
            },
            "apenasMeus": {
                "type": "string",
                "description": "Return only processes assigned to the current user ('S'/'N'). Optional for: processo.listar.",
            },
            # ── Document model filters ────────────────────────────────────────
            "grupoProtocoloModelo": {
                "type": "string",
                "description": "Document model group ID. Optional for: modelo_documento.listar.",
            },
            "tipoFiltro": {
                "type": "string",
                "description": "Filter type. Optional for: modelo_documento.listar.",
            },
            # ── Special tracking ──────────────────────────────────────────────
            "grupoAcompanhamento": {
                "type": "string",
                "description": "Tracking group. Optional for: acompanhamento_especial.listar.",
            },
            # ── Session unit override ─────────────────────────────────────────
            "unidade": {
                "type": "string",
                "description": (
                    "Unit ID to override the default session unit context for this "
                    "specific request. Useful when querying data from a different unit."
                ),
            },
        },
        "required": ["operation"],
        "additionalProperties": False,
    }

    # ── Tool config (stored encrypted in DB) ─────────────────────────────────

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
                    "SEI-PEN environment preset. Maps to the standard WSSEI v2 URL pattern. "
                    "Use 'base_url' to override when your installation uses a non-standard URL."
                ),
            },
            "base_url": {
                "type": "string",
                "description": (
                    "Custom WSSEI v2 base URL. Overrides 'ambiente' when set. "
                    "Example: http://sei.myorg.gov.br/sei/modulos/wssei/controlador_ws.php/api/v2"
                ),
            },
            "usuario": {
                "type": "string",
                "description": "SEI username (login/sigla).",
            },
            "senha": {
                "type": "string",
                "description": "SEI password. Stored encrypted.",
            },
            "orgao_id": {
                "type": "string",
                "description": "SEI organ/agency numeric ID (e.g. '0' for the default organ).",
            },
            "unidade_id": {
                "type": "string",
                "description": "Default unit ID sent with every request to maintain session context.",
            },
        },
        "required": ["usuario", "senha", "orgao_id"],
        "additionalProperties": False,
    }
    config_defaults: ClassVar[dict] = {}

    # ── No persistent state needed ────────────────────────────────────────────

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Execute ───────────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.perf_counter()
        operation: str = params["operation"]

        # ── Resolve base URL ──────────────────────────────────────────────────
        try:
            base_url = resolve_base_url(
                config.get("ambiente"),
                config.get("base_url"),
            )
        except SeiPenError as exc:
            return self._failure("CONFIG_ERROR", str(exc))

        # ── Validate credentials ──────────────────────────────────────────────
        usuario: str = config.get("usuario", "").strip()
        senha: str = config.get("senha", "").strip()
        orgao_id: str = str(config.get("orgao_id", "")).strip()
        if not usuario or not senha or not orgao_id:
            return self._failure(
                "CONFIG_ERROR",
                "Tool config must include 'usuario', 'senha', and 'orgao_id'.",
            )

        # ── Build request ─────────────────────────────────────────────────────
        try:
            method, path, query, form = build_request(operation, params, is_write=False)
        except BuildError as exc:
            return self._failure("INVALID_PARAMS", str(exc))

        # ── Execute API call ──────────────────────────────────────────────────
        client = SeiPenClient(
            base_url=base_url,
            usuario=usuario,
            senha=senha,
            orgao_id=orgao_id,
            unidade_id=config.get("unidade_id"),
            timeout=float(self.timeout_seconds),
        )
        unidade_override: Optional[str] = params.get("unidade")

        try:
            response = await client.request(
                method,
                path,
                params=query or None,
                data=form or None,
                unidade_override=unidade_override,
            )
        except SeiPenError as exc:
            log.warning(
                "sei_pen_read: operation=%s error=%s", operation, exc, exc_info=True
            )
            retryable = exc.status_code in (429, 500, 502, 503, 504) if exc.status_code else True
            return self._failure(
                "SEI_API_ERROR",
                str(exc),
                retryable=retryable,
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        log.info(
            "sei_pen_read: operation=%s status=success elapsed_ms=%d",
            operation,
            elapsed_ms,
        )
        return self._success(
            {
                "operation": operation,
                "result": response.get("data"),
                "total": response.get("total"),
                "sucesso": response.get("sucesso"),
            }
        )
