"""gSage AI — SEI-PEN Write tool.

Performs write operations on the SEI (Sistema Eletrônico de Informações)
WSSEI REST API v2: acknowledge documents, create/update internal documents,
and create/update processes.

All operations require **human-in-the-loop approval** before execution.

Required permission: ``sei:write``
"""

from __future__ import annotations

import json as _json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import ClassVar, Optional

import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.elasticsearch.client import ElasticsearchClient
from src.shared.security.context import AgentContext

from custom_code.tools.sei_pen import _helpers
from custom_code.tools.sei_pen._client import (
    SeiPenClient,
    SeiPenError,
    resolve_base_url,
    with_hint,
)
from custom_code.tools.sei_pen._helpers import HelperError
from custom_code.tools.sei_pen._operations import BuildError, WRITE_OPERATIONS, build_request

log = logging.getLogger(__name__)

# High-level helper operations (multi-call chains / name resolution). These are
# not single-call WSSEI routes, so they are dispatched before ``build_request``.
HELPER_OPS = (
    "processo.criar_facil",
    "documento.criar_com_conteudo",
    "documento.atualizar_conteudo",
    "processo.definir_prazo",
)

_WRITE_OP_IDS = sorted(WRITE_OPERATIONS.keys()) + sorted(HELPER_OPS)

# ── Per-coroutine session transport ───────────────────────────────────────────
# Bridges the DB session into execute() (which has no `session` param) so the
# cached reference-data loaders used by name-resolution helpers receive it.
_session_ctx: ContextVar[Optional[AsyncSession]] = ContextVar(
    "sei_pen_write_session", default=None
)


class SeiPenWriteTool(BaseTool):
    """Perform write operations on the SEI WSSEI REST API v2.

    All operations require human-in-the-loop approval before execution.

    **Available operations**

    | Operation                      | Description                                                    |
    |--------------------------------|----------------------------------------------------------------|
    | ``documento.dar_ciencia``      | Acknowledge a document                                         |
    | ``documento.cadastrar_interno``| Create a new internal document inside a process               |
    | ``documento.alterar_interno``  | Update metadata of an existing internal document              |
    | ``processo.criar``             | Create a new SEI process                                       |
    | ``processo.alterar``           | Update metadata of an existing process                         |
    | ``documento.atualizar_conteudo`` | High-level: rewrite editable section(s) of an existing doc in one batched call |

    **Required params per operation**

    *documento.dar_ciencia*: ``documento`` (document internal ID)

    *documento.cadastrar_interno*: ``procedimento``, ``idSerie``, ``observacao``, ``nivelAcesso``
    (``idUnidadeGeradoraProtocolo`` defaults to the session unit when omitted)

    *documento.alterar_interno*: ``documento``, ``observacao``, ``nivelAcesso``

    *processo.criar*: ``tipoProcesso``, ``nivelAcesso``, ``hipoteseLegal``

    *processo.alterar*: ``protocolo``, ``idTipoProcesso``, ``nivelAcesso``

    **Access level values**: 0 = público, 1 = restrito, 2 = sigiloso

    Requires **human approval** before execution.

    Permission: ``sei:write``
    """

    name: ClassVar[str] = "sei_pen_write"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Create and update documents and processes in the SEI government "
        "document management system via the WSSEI REST API v2 (requires approval)"
    )
    category: ClassVar[str] = "document"
    permissions: ClassVar[list[str]] = ["sei:write"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True
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

    audit_field_mapping: ClassVar[dict] = {"target_entities": "protocolo"}

    # ── Tool params ───────────────────────────────────────────────────────────

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": _WRITE_OP_IDS,
                "description": (
                    "SEI-PEN write operation to perform. "
                    "See tool description for required params per operation."
                ),
            },
            # ── Identifiers ───────────────────────────────────────────────────
            "procedimento": {
                "type": "string",
                "description": (
                    "Process internal numeric ID. "
                    "Required for: documento.cadastrar_interno."
                ),
            },
            "documento": {
                "type": "string",
                "description": (
                    "Document internal numeric ID. "
                    "Required for: documento.dar_ciencia, documento.alterar_interno, "
                    "documento.atualizar_conteudo."
                ),
            },
            "protocolo": {
                "type": "string",
                "description": (
                    "Process internal ID or formatted protocol. "
                    "Required for: processo.alterar."
                ),
            },
            # ── Document creation / update ────────────────────────────────────
            "idSerie": {
                "type": "string",
                "description": (
                    "Document type (série) ID. "
                    "Use sei_pen_read(operation='unidade.pesquisar') to discover available types. "
                    "Required for: documento.cadastrar_interno."
                ),
            },
            "observacao": {
                "type": "string",
                "description": (
                    "Observation text attached to the document. "
                    "Required for: documento.cadastrar_interno, documento.alterar_interno."
                ),
            },
            "nivelAcesso": {
                "type": "integer",
                "minimum": 0,
                "maximum": 2,
                "description": (
                    "Access level: 0 = público, 1 = restrito, 2 = sigiloso. "
                    "Required for: documento.cadastrar_interno, documento.alterar_interno, "
                    "processo.criar, processo.alterar."
                ),
            },
            "assuntos": {
                "type": "string",
                "description": (
                    "Comma-separated subject IDs, e.g. '3,5'. "
                    "Optional for: documento.cadastrar_interno, documento.alterar_interno, "
                    "processo.criar, processo.alterar."
                ),
            },
            "interessados": {
                "type": "string",
                "description": (
                    "Comma-separated interested-party (contato) IDs. "
                    "Optional for: documento.cadastrar_interno, documento.alterar_interno, "
                    "processo.criar, processo.alterar."
                ),
            },
            "idHipoteseLegal": {
                "type": "string",
                "description": (
                    "Legal hypothesis ID (required when nivelAcesso > 0). "
                    "Optional for: documento.cadastrar_interno, documento.alterar_interno, "
                    "processo.alterar."
                ),
            },
            "protocoloDocumentoModelo": {
                "type": "string",
                "description": (
                    "Formatted protocol of the model document to use as template. "
                    "Optional for: documento.cadastrar_interno."
                ),
            },
            "descricao": {
                "type": "string",
                "description": (
                    "Free-text description for the document. "
                    "Optional for: documento.cadastrar_interno, documento.alterar_interno."
                ),
            },
            "destinatarios": {
                "type": "string",
                "description": (
                    "Comma-separated recipient (contato) IDs. "
                    "Optional for: documento.cadastrar_interno, documento.alterar_interno."
                ),
            },
            "idUnidadeGeradoraProtocolo": {
                "type": "string",
                "description": (
                    "Generating/responsible unit ID for the document. "
                    "When omitted, defaults to the session unit ('unidade_id' from the "
                    "credential, or the 'unidade' override). The WSSEI module requires "
                    "this field to register the document. "
                    "Optional for: documento.cadastrar_interno, documento.alterar_interno."
                ),
            },
            # ── Process creation / update ─────────────────────────────────────
            "tipoProcesso": {
                "type": "string",
                "description": (
                    "Process type ID. "
                    "Use sei_pen_read(operation='processo.pesquisar_assunto') to discover. "
                    "Required for: processo.criar."
                ),
            },
            "hipoteseLegal": {
                "type": "string",
                "description": (
                    "Legal hypothesis value for the process. "
                    "Required for: processo.criar."
                ),
            },
            "grauSigilo": {
                "type": "string",
                "description": (
                    "Secrecy degree. Only relevant for sigiloso processes "
                    "(nivelAcesso=2). Omit or pass empty string ('') for public "
                    "or restricted processes. "
                    "Optional for: processo.criar, processo.alterar."
                ),
            },
            "especificacao": {
                "type": "string",
                "description": (
                    "Process subject/specification text. "
                    "Optional for: processo.criar, processo.alterar."
                ),
            },
            "observacoes": {
                "type": "string",
                "description": (
                    "Free-text observations for the process. "
                    "Optional for: processo.criar."
                ),
            },
            "idTipoProcesso": {
                "type": "string",
                "description": (
                    "Process type ID to update. "
                    "Required for: processo.alterar."
                ),
            },
            # ── Document section content ──────────────────────────────────────
            "versao": {
                "type": "string",
                "description": (
                    "Document version number to edit. "
                    "Required for: documento.secao_alterar. "
                    "Get it from sei_pen_read(operation='documento.secao_listar')."
                ),
            },
            "secoes": {
                "description": (
                    "Sections to update. "
                    "For documento.secao_alterar: a JSON-encoded string of "
                    '[{"id", "idSecaoModelo", "conteudo"}]. '
                    "For documento.atualizar_conteudo: a JSON array (object[]) "
                    "of {id|idSecaoModelo, conteudo}. "
                    "Required for: documento.secao_alterar; "
                    "alternative to 'conteudo_html' for documento.atualizar_conteudo."
                ),
            },
            # ── Process tramitation / lifecycle ───────────────────────────────
            "numeroProcesso": {
                "type": "string",
                "description": (
                    "Formatted process number/protocol. "
                    "Required for: processo.enviar, processo.concluir, processo.atribuir."
                ),
            },
            "unidadesDestino": {
                "type": "string",
                "description": (
                    "Comma-separated destination unit IDs, e.g. '110000965,110000966'. "
                    "Required for: processo.enviar."
                ),
            },
            "usuario": {
                "type": "string",
                "description": (
                    "User ID/login. "
                    "Required for: processo.atribuir. "
                    "Optional for: processo.agendar_retorno, processo.definir_prazo."
                ),
            },
            "dtProgramada": {
                "type": "string",
                "description": (
                    "Programmed return date in dd/MM/yyyy. "
                    "Required for: processo.agendar_retorno."
                ),
            },
            "atividadeEnvio": {
                "type": "string",
                "description": (
                    "Sending activity ID associated with the scheduled return. "
                    "Optional for: processo.agendar_retorno, processo.definir_prazo."
                ),
            },
            "protocoloDestino": {
                "type": "string",
                "description": (
                    "Related process protocol when suspending. "
                    "Optional for: processo.sobrestar."
                ),
            },
            "motivo": {
                "type": "string",
                "description": (
                    "Reason text. Optional for: processo.sobrestar."
                ),
            },
            "nome": {
                "type": "string",
                "description": (
                    "Contact (interested party) name. Required for: contato.criar."
                ),
            },
            "sinManterAbertoUnidade": {
                "type": "string",
                "description": (
                    "'S'/'N' — keep the process open in the current unit. "
                    "Optional for: processo.enviar."
                ),
            },
            "sinRemoverAnotacao": {
                "type": "string",
                "description": "'S'/'N'. Optional for: processo.enviar.",
            },
            "sinEnviarEmailNotificacao": {
                "type": "string",
                "description": "'S'/'N'. Optional for: processo.enviar.",
            },
            "dataRetornoProgramado": {
                "type": "string",
                "description": (
                    "dd/MM/yyyy return date on send. Optional for: processo.enviar."
                ),
            },
            "diasRetornoProgramado": {
                "type": "string",
                "description": (
                    "Number of days for return on send. Optional for: processo.enviar."
                ),
            },
            "sinDiasUteisRetornoProgramado": {
                "type": "string",
                "description": "'S'/'N'. Optional for: processo.enviar.",
            },
            "sinReabrir": {
                "type": "string",
                "description": "'S'/'N'. Optional for: processo.enviar.",
            },
            # ── High-level helper inputs (friendly, name-based) ───────────────
            "tipo_processo_nome": {
                "type": "string",
                "description": (
                    "Process type *name* to resolve automatically. "
                    "Used by: processo.criar_facil (alternative to tipoProcesso)."
                ),
            },
            "serie_nome": {
                "type": "string",
                "description": (
                    "Document type (série) *name* to resolve automatically. "
                    "Used by: documento.criar_com_conteudo (alternative to idSerie)."
                ),
            },
            "hipotese_nome": {
                "type": "string",
                "description": (
                    "Legal-hypothesis *name* to resolve automatically. "
                    "Used by: processo.criar_facil (alternative to hipoteseLegal)."
                ),
            },
            "conteudo_html": {
                "type": "string",
                "description": (
                    "HTML body content to write into the document. "
                    "Required for: documento.criar_com_conteudo. "
                    "Quick-mode alternative to 'secoes' for documento.atualizar_conteudo "
                    "(overwrites the editable principal section)."
                ),
            },
            "dias": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Days ahead to compute the deadline date. "
                    "Used by: processo.definir_prazo (alternative to dtProgramada)."
                ),
            },
            "data": {
                "type": "string",
                "description": (
                    "Explicit deadline date in dd/MM/yyyy. "
                    "Used by: processo.definir_prazo (alternative to dias)."
                ),
            },
            # ── Session unit override ─────────────────────────────────────────
            "unidade": {
                "type": "string",
                "description": (
                    "Unit ID to override the default session unit context for this request."
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
                    "SEI-PEN environment preset. Maps to the standard WSSEI v2 URL. "
                    "Use 'base_url' for non-standard installations."
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

    # ── Session transport ─────────────────────────────────────────────────────
    async def run(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        redis_client: redis.Redis,
        es_client: ElasticsearchClient,
        gsage_session_id: Optional[uuid.UUID] = None,
    ) -> ToolResult:
        """Override to make the DB session available inside execute().

        The cached SEI reference-data loaders (used by name-resolution helpers)
        require a ``session`` to read/write the result cache.
        """
        token = _session_ctx.set(session)
        try:
            return await super().run(
                agent_context,
                params,
                session,
                redis_client,
                es_client,
                gsage_session_id,
            )
        finally:
            _session_ctx.reset(token)

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

        # ── Build request ─────────────────────────────────────────────────────
        # Default idUnidadeGeradoraProtocolo to the session unit for internal
        # document operations when the caller does not provide it. The WSSEI
        # module sets the document's generating/responsible unit from this
        # field; omitting it makes the underlying SEI component reject the
        # request with an empty error message.
        if operation in ("documento.cadastrar_interno", "documento.alterar_interno"):
            if not params.get("idUnidadeGeradoraProtocolo"):
                default_unit = params.get("unidade") or unidade_id
                if default_unit:
                    params = {**params, "idUnidadeGeradoraProtocolo": default_unit}

        # ── Build the REST client ─────────────────────────────────────────────
        client = SeiPenClient(
            base_url=base_url,
            usuario=usuario,
            senha=senha,
            orgao_id=orgao_id,
            unidade_id=unidade_id,
            timeout=float(self.timeout_seconds),
        )
        unidade_override: Optional[str] = params.get("unidade")

        # ── High-level helper chains (name resolution / multi-call) ───────────
        if operation in HELPER_OPS:
            session = _session_ctx.get()
            org_id = getattr(agent_context, "org_id", None)
            try:
                helper_result = await self._run_helper(
                    operation=operation,
                    params=params,
                    client=client,
                    base_url=base_url,
                    orgao_id=orgao_id,
                    org_id=org_id,
                    session=session,
                    unidade_override=unidade_override,
                    default_unit=unidade_override or unidade_id,
                )
            except HelperError as exc:
                msg = str(exc)
                if exc.candidates:
                    msg += "\n\nCandidates (use one of these ids):\n" + _json.dumps(
                        exc.candidates, ensure_ascii=False, indent=2
                    )
                return self._failure(
                    "INVALID_PARAMS",
                    msg,
                    execution_time_ms=round((time.perf_counter() - t0) * 1000),
                )
            except SeiPenError as exc:
                log.warning(
                    "sei_pen_write: helper=%s error=%s", operation, exc, exc_info=True
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
                "sei_pen_write: helper=%s status=success elapsed_ms=%d",
                operation,
                elapsed_ms,
            )
            return self._success(
                {"operation": operation, "result": helper_result},
                execution_time_ms=elapsed_ms,
            )

        # ── Single-call WSSEI route ───────────────────────────────────────────
        try:
            method, path, query, form = build_request(operation, params, is_write=True)
        except BuildError as exc:
            return self._failure("INVALID_PARAMS", str(exc))

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
                "sei_pen_write: operation=%s error=%s", operation, exc, exc_info=True
            )
            retryable = exc.status_code in (429, 500, 502, 503, 504) if exc.status_code else False
            return self._failure(
                "SEI_API_ERROR",
                with_hint(str(exc), exc.status_code, operation),
                retryable=retryable,
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        log.info(
            "sei_pen_write: operation=%s status=success elapsed_ms=%d",
            operation,
            elapsed_ms,
        )

        return self._success(
            {
                "operation": operation,
                "result": response.get("data"),
                "mensagem": response.get("mensagem"),
                "sucesso": response.get("sucesso"),
            }
        )

    # ── Helper dispatch ───────────────────────────────────────────────────────

    async def _run_helper(
        self,
        *,
        operation: str,
        params: dict,
        client: SeiPenClient,
        base_url: str,
        orgao_id: str,
        org_id: Optional[uuid.UUID],
        session: Optional[AsyncSession],
        unidade_override: Optional[str],
        default_unit: Optional[str],
    ) -> dict:
        """Dispatch a high-level helper chain by operation name."""
        if operation == "processo.criar_facil":
            return await _helpers.criar_processo_facil(
                client=client,
                base_url=base_url,
                orgao_id=orgao_id,
                org_id=org_id,
                session=session,
                unidade_override=unidade_override,
                tipo_processo_nome=params.get("tipo_processo_nome"),
                tipo_processo_id=params.get("tipoProcesso"),
                nivel_acesso=int(params.get("nivelAcesso", 0) or 0),
                hipotese_nome=params.get("hipotese_nome"),
                hipotese_id=params.get("hipoteseLegal"),
                grau_sigilo=params.get("grauSigilo", "") or "",
                especificacao=params.get("especificacao"),
                interessados=params.get("interessados"),
                assuntos=params.get("assuntos"),
                observacoes=params.get("observacoes"),
            )

        if operation == "documento.criar_com_conteudo":
            procedimento = params.get("procedimento")
            if not procedimento:
                raise HelperError(
                    "'procedimento' (process internal ID) is required for "
                    "documento.criar_com_conteudo."
                )
            conteudo = params.get("conteudo_html")
            if not conteudo:
                raise HelperError(
                    "'conteudo_html' is required for documento.criar_com_conteudo."
                )
            return await _helpers.criar_documento_com_conteudo(
                client=client,
                base_url=base_url,
                orgao_id=orgao_id,
                org_id=org_id,
                session=session,
                unidade_override=unidade_override,
                procedimento=str(procedimento),
                serie_nome=params.get("serie_nome"),
                id_serie=params.get("idSerie"),
                nivel_acesso=int(params.get("nivelAcesso", 0) or 0),
                observacao=params.get("observacao", "") or "",
                conteudo_html=str(conteudo),
                id_unidade_geradora=params.get("idUnidadeGeradoraProtocolo")
                or default_unit,
                id_hipotese_legal=params.get("idHipoteseLegal"),
            )

        if operation == "processo.definir_prazo":
            dias_raw = params.get("dias")
            return await _helpers.definir_prazo(
                client=client,
                unidade_override=unidade_override,
                unidade=params.get("unidade"),
                dias=int(dias_raw) if dias_raw not in (None, "") else None,
                data=params.get("data") or params.get("dtProgramada"),
                usuario=params.get("usuario"),
                atividade_envio=params.get("atividadeEnvio"),
            )

        if operation == "documento.atualizar_conteudo":
            documento = params.get("documento")
            if not documento:
                raise HelperError(
                    "'documento' (internal document ID) is required for "
                    "documento.atualizar_conteudo."
                )
            raw_secoes = params.get("secoes")
            secoes_arg: Optional[list[dict]] = None
            if raw_secoes:
                if isinstance(raw_secoes, list):
                    secoes_arg = raw_secoes
                elif isinstance(raw_secoes, str):
                    try:
                        parsed = _json.loads(raw_secoes)
                    except _json.JSONDecodeError as exc:
                        raise HelperError(
                            f"'secoes' is not valid JSON: {exc}"
                        ) from exc
                    if not isinstance(parsed, list):
                        raise HelperError(
                            "'secoes' must decode to a JSON array of section objects."
                        )
                    secoes_arg = parsed
                else:
                    raise HelperError(
                        "'secoes' must be a list of section objects or a JSON-encoded "
                        "string of that list."
                    )
            return await _helpers.atualizar_documento_conteudo(
                client=client,
                unidade_override=unidade_override,
                documento=str(documento),
                conteudo_html=params.get("conteudo_html"),
                secoes=secoes_arg,
            )

        raise HelperError(f"Unknown helper operation: {operation}")
