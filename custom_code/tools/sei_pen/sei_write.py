"""gSage AI — SEI-PEN Write tool.

Performs write operations on the SEI (Sistema Eletrônico de Informações)
WSSEI REST API v2: acknowledge documents, create/update internal documents,
and create/update processes.

All operations require **human-in-the-loop approval** before execution.

Required permission: ``sei:write``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

from custom_code.tools.sei_pen._client import SeiPenClient, SeiPenError, resolve_base_url
from custom_code.tools.sei_pen._operations import BuildError, WRITE_OPERATIONS, build_request

log = logging.getLogger(__name__)

_WRITE_OP_IDS = sorted(WRITE_OPERATIONS.keys())


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

    **Required params per operation**

    *documento.dar_ciencia*: ``documento`` (document internal ID)

    *documento.cadastrar_interno*: ``procedimento``, ``idSerie``, ``observacao``, ``nivelAcesso``

    *documento.alterar_interno*: ``documento``, ``observacao``, ``nivelAcesso``

    *processo.criar*: ``tipoProcesso``, ``nivelAcesso``, ``hipoteseLegal``, ``grauSigilo``

    *processo.alterar*: ``protocolo``, ``idTipoProcesso``, ``nivelAcesso``, ``grauSigilo``

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
        "description": "SEI-PEN username and password (login/sigla and senha).",
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
                    "Required for: documento.dar_ciencia, documento.alterar_interno."
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
                    "Secrecy degree. Pass empty string ('') for public or restricted processes. "
                    "Required for: processo.criar, processo.alterar."
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
            "orgao_id": {
                "type": "string",
                "description": "SEI organ/agency numeric ID (e.g. '0' for the default organ).",
            },
            "unidade_id": {
                "type": "string",
                "description": "Default unit ID sent with every request to maintain session context.",
            },
        },
        "required": ["orgao_id"],
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
        orgao_id: str = str(config.get("orgao_id", "")).strip()
        if not usuario or not senha:
            return self._failure(
                "CREDENTIAL_MISSING",
                "SEI-PEN requires a personal credential. Configure your "
                "'sei_pen' credential in Settings → Credentials and link "
                "it as active for this tool.",
            )
        if not orgao_id:
            return self._failure(
                "CONFIG_ERROR",
                "Tool config must include 'orgao_id'.",
            )

        # ── Build request ─────────────────────────────────────────────────────
        try:
            method, path, query, form = build_request(operation, params, is_write=True)
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
                "sei_pen_write: operation=%s error=%s", operation, exc, exc_info=True
            )
            retryable = exc.status_code in (429, 500, 502, 503, 504) if exc.status_code else True
            return self._failure(
                "SEI_API_ERROR",
                str(exc),
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
