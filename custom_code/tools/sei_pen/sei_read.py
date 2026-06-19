"""gSage AI — SEI-PEN Read tool.

Queries the SEI (Sistema Eletrônico de Informações) WSSEI REST API v2 for
read-only information: organs, processes, documents, units, document models,
tracking groups, special tracking, and users.

Required permission: ``sei:read``
"""

from __future__ import annotations

import json
import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

from custom_code.tools.sei_pen._client import (
    SeiPenClient,
    SeiPenError,
    resolve_base_url,
    with_hint,
)
from custom_code.tools.sei_pen._helpers import (
    HelperError,
    _resolve_protocolo,
    listar_processos_facil,
    ver_documento_completo,
    ver_processo_completo,
)
from custom_code.tools.sei_pen._operations import BuildError, READ_OPERATIONS, build_request

log = logging.getLogger(__name__)

# High-level helper operations (orchestrate multiple WSSEI calls into one).
READ_HELPER_OPS = (
    "documento.ver_completo",
    "processo.listar_facil",
    "processo.ver_completo",
)

# Operations whose ``protocolo`` parameter accepts a formatted SEI protocol
# number (e.g. "000123.000002/2026-46") and needs transparent resolution
# to a numeric internal ID via ``processo.pesquisar_geral``.
_PROTOCOLO_OPS: set[str] = {
    "processo.consultar",
    "processo.consultar_atribuicao",
    "processo.consultar_acompanhamento",
    "processo.relacionamentos",
    "processo.interessados_listar",
    "processo.unidades_listar",
    "processo.ciencia_listar",
    "processo.sobrestamento_listar",
}

# Sorted list of all read operation IDs for the params schema enum
_READ_OP_IDS = sorted(READ_OPERATIONS.keys()) + sorted(READ_HELPER_OPS)


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
    | ``processo.listar_facil``               | Agent-friendly listing with smart defaults + hints     |
    | ``processo.consultar``                 | Retrieve full details of a process by protocol        |
    | ``processo.consultar_atribuicao``      | Retrieve assignment info of a process                 |
    | ``processo.consultar_acompanhamento``  | Retrieve tracking details for a process               |
    | ``grupo_acompanhamento.listar``        | List tracking groups                                  |
    | ``modelo_documento.listar_grupo``      | List document model groups                            |
    | ``modelo_documento.listar``            | List document models                                  |
    | ``acompanhamento_especial.listar``     | List special tracking entries                         |
    | ``usuario.pesquisar``                  | Search users by keyword                               |
    | ``usuario.listar_unidades``            | List units accessible to a user                       |
    | ``hipotese_legal.pesquisar``           | Search legal hypotheses by access level               |
    | ``documento.ver_completo``             | High-level: metadata + rendered HTML + sections in one call |
    | ``processo.ver_completo``              | High-level: process metadata + documents with per-doc metadata |

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
                    "modelo_documento.listar_grupo, modelo_documento.listar. "
                    "Note: processo.listar does NOT support a text filter."
                ),
            },
            "id": {
                "type": "string",
                "description": (
                    "ID for detail lookup. Optional for: unidade.pesquisar_outras, "
                    "unidade.pesquisar_texto_padrao, processo.pesquisar_assunto, "
                    "grupo_acompanhamento.listar, modelo_documento.listar_grupo, "
                    "modelo_documento.listar, processo.listar, "
                    "documento.secao_listar."
                ),
            },
            # ── Document params ───────────────────────────────────────────────
            "documento": {
                "type": "string",
                "description": (
                    "Internal document ID (numeric). "
                    "Required for: documento.visualizar, documento.ver_completo."
                ),
            },
            "protocolo": {
                "type": "string",
                "description": (
                    "Document or process protocol / internal ID. "
                    "Required for: documento.consultar_interno, processo.consultar, "
                    "processo.consultar_atribuicao, processo.consultar_acompanhamento, "
                    "processo.ver_completo."
                ),
            },
            "procedimento": {
                "type": "string",
                "description": (
                    "Process internal ID (numeric). "
                    "Required for: documento.listar_em_processo, atividade.listar."
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
                "enum": ["R", "G"],
                "description": (
                    "Search-mode flag for processo.listar (NOT a process-type "
                    "filter): 'R' = received processes, 'G' = generated processes. "
                    "Omit to list all. Optional for: processo.listar."
                ),
            },
            "apenasMeus": {
                "type": "string",
                "description": "Return only processes assigned to the current user ('S'/'N'). Optional for: processo.listar.",
            },
            # ── High-level helper params (processo.listar_facil) ──────────
            "apenas_meus": {
                "type": "boolean",
                "description": (
                    "Show only the current user's processes. "
                    "Optional for: processo.listar_facil (default: true)."
                ),
            },
            "id_unidade": {
                "type": "string",
                "description": (
                    "Filter by unit ID (numeric). "
                    "Optional for: processo.listar_facil."
                ),
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
            # ── Legal hypothesis search ───────────────────────────────────────
            "nivelAcesso": {
                "type": "integer",
                "minimum": 0,
                "maximum": 2,
                "description": (
                    "Access level filter: 0 = público, 1 = restrito, 2 = sigiloso. "
                    "Required for: hipotese_legal.pesquisar."
                ),
            },
            # ── Reference / discovery lookups ─────────────────────────────────
            "favoritos": {
                "type": "string",
                "description": (
                    "Return only favourite entries ('S'/'N'). "
                    "Optional for: processo.tipo_listar, documento.tipo_pesquisar."
                ),
            },
            "aplicabilidade": {
                "type": "string",
                "description": (
                    "Comma-separated applicability filters for document types. "
                    "Optional for: documento.tipo_pesquisar."
                ),
            },
            "idGrupoContato": {
                "type": "string",
                "description": "Contact group ID. Optional for: contato.pesquisar.",
            },
            "tipoProcedimento": {
                "type": "string",
                "description": (
                    "Process type ID. Required for: processo.assunto_sugestao."
                ),
            },
            "serie": {
                "type": "string",
                "description": (
                    "Document type (série) ID. Required for: documento.assunto_sugestao."
                ),
            },
            # ── Session unit override ─────────────────────────────────────────
            "unidade": {
                "type": "string",
                "description": (
                    "Unit ID to override the default session unit context for this "
                    "specific request. Useful when querying data from a different unit."
                ),
            },
            # ── High-level helper params (documento.ver_completo) ─────────────
            "incluir_visualizacao": {
                "type": "boolean",
                "description": (
                    "Whether to include the rendered HTML view in the result. "
                    "Optional for: documento.ver_completo (default: true)."
                ),
            },
            "incluir_secoes": {
                "type": "boolean",
                "description": (
                    "Whether to include the structured editable sections in the result. "
                    "Optional for: documento.ver_completo (default: true)."
                ),
            },
            # ── High-level helper params (processo.ver_completo) ──────────
            "incluir_documentos": {
                "type": "boolean",
                "description": (
                    "Whether to include the document listing in the result. "
                    "Optional for: processo.ver_completo (default: true)."
                ),
            },
            "incluir_metadados_documentos": {
                "type": "boolean",
                "description": (
                    "Whether to fetch per-document metadata (one extra API call each). "
                    "Optional for: processo.ver_completo (default: true)."
                ),
            },
            "documento_limit": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Max documents to return from the listing. "
                    "Optional for: processo.ver_completo."
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
        },
        "required": [],
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

        # ── Build the REST client (needed for both helpers and single-call) ──
        client = SeiPenClient(
            base_url=base_url,
            usuario=usuario,
            senha=senha,
            orgao_id=orgao_id,
            unidade_id=unidade_id,
            timeout=float(self.timeout_seconds),
        )
        unidade_override: Optional[str] = params.get("unidade")

        # ── Resolve formatted protocol → numeric internal ID ──────────────────
        if operation in _PROTOCOLO_OPS:
            protocolo = params.get("protocolo")
            if protocolo:
                try:
                    params["protocolo"] = await _resolve_protocolo(
                        client,
                        str(protocolo),
                        unidade_override=unidade_override,
                    )
                except HelperError as exc:
                    msg = str(exc)
                    if exc.candidates:
                        msg += "\n\nCandidates (use one of these ids):\n" + json.dumps(
                            exc.candidates, ensure_ascii=False, indent=2
                        )
                    return self._failure(
                        "INVALID_PARAMS",
                        msg,
                        execution_time_ms=round((time.perf_counter() - t0) * 1000),
                    )
            # Also resolve protocolo for processo.ver_completo (handled below).

        # ── High-level helper operations (orchestrate multiple WSSEI calls) ──
        if operation in READ_HELPER_OPS:
            try:
                helper_result = await self._run_helper(
                    operation=operation,
                    params=params,
                    client=client,
                    unidade_override=unidade_override,
                )
            except HelperError as exc:
                msg = str(exc)
                if exc.candidates:
                    msg += "\n\nCandidates (use one of these ids):\n" + json.dumps(
                        exc.candidates, ensure_ascii=False, indent=2
                    )
                return self._failure(
                    "INVALID_PARAMS",
                    msg,
                    execution_time_ms=round((time.perf_counter() - t0) * 1000),
                )
            except SeiPenError as exc:
                log.warning(
                    "sei_pen_read: helper=%s error=%s", operation, exc, exc_info=True
                )
                retryable = (
                    exc.status_code in (429, 500, 502, 503, 504)
                    if exc.status_code
                    else False
                )
                return self._failure(
                    "SEI_API_ERROR",
                    with_hint(str(exc), exc.status_code, operation),
                    retryable=retryable,
                )

            elapsed_ms = round((time.perf_counter() - t0) * 1000)
            log.info(
                "sei_pen_read: helper=%s status=success elapsed_ms=%d",
                operation,
                elapsed_ms,
            )
            return self._success(
                {"operation": operation, "result": helper_result},
                execution_time_ms=elapsed_ms,
            )

        # ── Build request ─────────────────────────────────────────────────────
        try:
            method, path, query, form = build_request(operation, params, is_write=False)
        except BuildError as exc:
            return self._failure("INVALID_PARAMS", str(exc))

        # ── Execute API call ──────────────────────────────────────────────────
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
            # processo.relacionamentos 404 → empty list (SEI returns 404
            # instead of an empty array when the process has no relationships).
            if operation == "processo.relacionamentos" and exc.status_code == 404:
                elapsed_ms = round((time.perf_counter() - t0) * 1000)
                return self._success(
                    {
                        "operation": operation,
                        "result": [],
                        "total": 0,
                        "hints": [
                            "This process has no related processes (relacionamentos). "
                            "This is normal — not all processes are linked to others."
                        ],
                    },
                    execution_time_ms=elapsed_ms,
                )
            # HTTP 429/5xx are transient; an API-level rejection (no HTTP status,
            # e.g. not-found / invalid ID) will not fix itself on retry.
            retryable = (
                exc.status_code in (429, 500, 502, 503, 504)
                if exc.status_code
                else False
            )
            return self._failure(
                "SEI_API_ERROR",
                with_hint(str(exc), exc.status_code, operation),
                retryable=retryable,
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        log.info(
            "sei_pen_read: operation=%s status=success elapsed_ms=%d",
            operation,
            elapsed_ms,
        )

        result_data = response.get("data")

        # Normalise null → [] for list/search operations so the agent always
        # gets a consistent array shape (SEI sometimes returns null for empty
        # results e.g. usuario.listar_unidades).
        if result_data is None and operation.startswith(("usuario.", "unidade.", "processo.listar", "processo.pesquisar", "documento.listar", "documento.tipo_", "grupo_", "modelo_", "acompanhamento_", "atividade.", "hipotese_", "contato.", "serie.", "orgao.")):
            result_data = []

        # Special case: documento.visualizar may return an empty string
        # for some document types / access levels. Append a hint so the
        # agent knows about the ver_completo alternative.
        hints = None
        if operation == "documento.visualizar" and isinstance(result_data, str) and not result_data.strip():
            hints = [
                "Document visualization returned empty — this is a known SEI "
                "quirk for some document types. Use "
                "sei_pen_read(operation='documento.ver_completo', documento='<id>') "
                "for metadata + rendered HTML + sections."
            ]

        result_payload: dict = {
            "operation": operation,
            "result": result_data,
            "total": response.get("total"),
            "sucesso": response.get("sucesso"),
        }
        if hints:
            result_payload["hints"] = hints

        return self._success(result_payload, execution_time_ms=elapsed_ms)

    # ── Helper dispatch ───────────────────────────────────────────────────────

    async def _run_helper(
        self,
        *,
        operation: str,
        params: dict,
        client: SeiPenClient,
        unidade_override: Optional[str],
    ) -> dict:
        """Dispatch a high-level read helper by operation name."""
        if operation == "documento.ver_completo":
            documento = params.get("documento")
            if not documento:
                raise HelperError(
                    "'documento' (internal document ID) is required for "
                    "documento.ver_completo."
                )
            return await ver_documento_completo(
                client=client,
                unidade_override=unidade_override,
                documento=str(documento),
                incluir_visualizacao=bool(
                    params.get("incluir_visualizacao", True)
                ),
                incluir_secoes=bool(params.get("incluir_secoes", True)),
            )

        if operation == "processo.listar_facil":
            return await listar_processos_facil(
                client=client,
                unidade_override=unidade_override,
                limit=int(params.get("limit", 10) or 10),
                start=int(params.get("start", 0) or 0),
                apenas_meus=bool(params.get("apenas_meus", True)),
                tipo=params.get("tipo"),
                usuario=params.get("usuario"),
                id_unidade=params.get("id_unidade"),
            )

        if operation == "processo.ver_completo":
            protocolo = params.get("protocolo")
            if not protocolo:
                raise HelperError(
                    "'protocolo' (process protocol / internal ID) is required for "
                    "processo.ver_completo."
                )
            # Resolve formatted protocol → numeric ID before the multi-call chain.
            protocolo = await _resolve_protocolo(
                client,
                str(protocolo),
                unidade_override=unidade_override,
            )
            return await ver_processo_completo(
                client=client,
                unidade_override=unidade_override,
                protocolo=protocolo,
                incluir_documentos=bool(
                    params.get("incluir_documentos", True)
                ),
                incluir_metadados_documentos=bool(
                    params.get("incluir_metadados_documentos", True)
                ),
                documento_limit=params.get("documento_limit"),
            )

        raise HelperError(f"Unknown read helper operation: {operation}")
