"""gSage AI — SEI Write tool.

Performs write operations on the SEI (Sistema Eletrônico de Informações)
SOAP webservice: creates processes, adds documents, and sends processes to
other units.

All actions require human-in-the-loop approval before execution.

Required permission: ``sei:write``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

from custom_code.tools.seiws._client import SEIClient, SEIError

log = logging.getLogger(__name__)


class SeiWriteTool(BaseTool):
    """Perform write operations on the SEI webservice.

    **Available actions:**

    | Action              | Description                                                 |
    |---------------------|-------------------------------------------------------------|
    | ``gerar_processo``  | Create a new SEI process                                    |
    | ``incluir_documento`` | Add a generated document to an existing process           |
    | ``enviar_processo`` | Send a process to one or more destination units             |

    **Action-specific parameters:**

    *gerar_processo*: ``id_tipo_procedimento`` (required), ``especificacao`` (required),
    ``nivel_acesso`` (default "0"), ``assuntos`` (optional list), ``interessados`` (optional list),
    ``observacao`` (optional), ``id_hipotese_legal`` (optional)

    *incluir_documento*: ``protocolo_procedimento`` (required), ``id_serie`` (required),
    ``tipo`` ("G" or "R", default "G"), ``descricao`` (optional), ``conteudo`` (HTML text,
    will be base64-encoded), ``nivel_acesso`` (default "0")

    *enviar_processo*: ``protocolo`` (required), ``unidades_destino`` (required list of unit IDs),
    ``sin_manter_aberto`` (default "N"), ``sin_enviar_email`` (default "N")

    **Access level codes**: "0" = public, "1" = restricted, "2" = secret

    Requires **human approval** before execution.

    Permission: ``sei:write``
    """

    name: ClassVar[str] = "sei_write"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Create and update documents, add comments, and manage processes in the SEI system"
    category: ClassVar[str] = "document"
    available: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["sei:write"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True

    # Shared org-level configuration namespace for the seiws (SOAP) tool family.
    config_namespace: ClassVar[Optional[str]] = "seiws"

    audit_field_mapping: ClassVar[dict] = {}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "gerar_processo",
                    "incluir_documento",
                    "enviar_processo",
                ],
                "description": "SEI write operation to perform.",
            },
            # ── gerar_processo ────────────────────────────────────────────
            "id_tipo_procedimento": {
                "type": "string",
                "description": (
                    "SEI process type ID. "
                    "Use sei_read(action='listar_tipos_procedimento') to discover valid IDs. "
                    "Required for action='gerar_processo'."
                ),
            },
            "especificacao": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": (
                    "Subject / description of the process (up to 100 characters). "
                    "Required for action='gerar_processo'."
                ),
            },
            "nivel_acesso": {
                "type": "string",
                "enum": ["0", "1", "2"],
                "default": "0",
                "description": (
                    "Access level: '0' = public (default), '1' = restricted, '2' = secret. "
                    "Used by: gerar_processo, incluir_documento."
                ),
            },
            "assuntos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["codigo"],
                    "properties": {
                        "codigo": {
                            "type": "string",
                            "description": "Structured subject code (CodigoEstruturado).",
                        },
                        "descricao": {
                            "type": "string",
                            "description": "Human-readable subject description.",
                        },
                    },
                    "additionalProperties": False,
                },
                "description": (
                    "List of subject codes for the process. "
                    "Used by action='gerar_processo'."
                ),
            },
            "interessados": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["sigla"],
                    "properties": {
                        "sigla": {
                            "type": "string",
                            "description": "Sigla (acronym) of the interested party.",
                        },
                        "nome": {
                            "type": "string",
                            "description": "Full name of the interested party.",
                        },
                    },
                    "additionalProperties": False,
                },
                "description": (
                    "List of interested parties. "
                    "Used by action='gerar_processo'."
                ),
            },
            "observacao": {
                "type": "string",
                "description": (
                    "Internal observation for the process. "
                    "Used by action='gerar_processo'."
                ),
            },
            "id_hipotese_legal": {
                "type": "string",
                "description": (
                    "Legal hypothesis ID required when nivel_acesso is '1' or '2'. "
                    "Used by action='gerar_processo'."
                ),
            },
            # ── incluir_documento ─────────────────────────────────────────
            "protocolo_procedimento": {
                "type": "string",
                "description": (
                    "Protocol number of the target process (e.g. '35014.000001/2020-31'). "
                    "Required for action='incluir_documento'."
                ),
            },
            "id_serie": {
                "type": "string",
                "description": (
                    "Series ID from SEI (document type within a unit). "
                    "Use sei_read(action='listar_series') to discover valid IDs. "
                    "Required for action='incluir_documento'."
                ),
            },
            "tipo": {
                "type": "string",
                "enum": ["G", "R"],
                "default": "G",
                "description": (
                    "Document origin: 'G' = generated (default), 'R' = received. "
                    "Used by action='incluir_documento'."
                ),
            },
            "descricao": {
                "type": "string",
                "description": (
                    "Short description displayed in the process document tree. "
                    "Used by action='incluir_documento'."
                ),
            },
            "conteudo": {
                "type": "string",
                "description": (
                    "HTML content of the document. Will be base64-encoded before submission. "
                    "Used by action='incluir_documento'."
                ),
            },
            # ── enviar_processo ───────────────────────────────────────────
            "protocolo": {
                "type": "string",
                "description": (
                    "Protocol number of the process to send. "
                    "Required for action='enviar_processo'."
                ),
            },
            "unidades_destino": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "List of destination unit IDs. "
                    "Use sei_read(action='listar_unidades') to discover valid IDs. "
                    "Required for action='enviar_processo'."
                ),
            },
            "sin_manter_aberto": {
                "type": "string",
                "enum": ["S", "N"],
                "default": "N",
                "description": (
                    "'S' to keep the process open in the origin unit after sending; "
                    "'N' to close it (default). "
                    "Used by action='enviar_processo'."
                ),
            },
            "sin_enviar_email": {
                "type": "string",
                "enum": ["S", "N"],
                "default": "N",
                "description": (
                    "'S' to send an e-mail notification to the destination unit; "
                    "'N' otherwise (default). "
                    "Used by action='enviar_processo'."
                ),
            },
            # ── common ────────────────────────────────────────────────────
            "id_unidade": {
                "type": "string",
                "description": (
                    "Unit ID override for this call. "
                    "If omitted, the default unit from config is used."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "SEI WSDL URL.",
            },
            "sigla_sistema": {
                "type": "string",
                "description": "System acronym registered in SEI admin panel.",
            },
            "identificacao_servico": {
                "type": "string",
                "description": "Access key (chave de acesso) for the system.",
            },
            "id_unidade": {
                "type": "string",
                "description": "Default unit ID for all SEI operations.",
            },
        },
        "additionalProperties": False,
    }
    config_defaults: ClassVar[dict] = {
        "url": "",
        "sigla_sistema": "",
        "identificacao_servico": "",
        "id_unidade": "",
    }
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
        t0 = time.monotonic()
        action: str = params["action"]
        log.debug(
            "sei_write: action=%s params=%r",
            action,
            {k: v for k, v in params.items() if k != "action"},
        )

        try:
            async with SEIClient(
                url=config.get("url") or None,
                sigla_sistema=config.get("sigla_sistema") or None,
                identificacao_servico=config.get("identificacao_servico") or None,
                id_unidade=config.get("id_unidade") or None,
            ) as client:
                result = await self._dispatch(client, action, params)
        except SEIError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.fault_code or "SEI_ERROR",
                str(exc),
                execution_time_ms=elapsed,
            )
        except ValueError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INVALID_PARAMS", str(exc), execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("sei_write: unexpected error in action=%s", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        log.debug("sei_write: action=%s OK in %dms", action, elapsed)
        return self._success(
            data={"action": action, "result": result},
            execution_time_ms=elapsed,
        )

    # ── Dispatcher ────────────────────────────────────────────────────────

    async def _dispatch(self, client: SEIClient, action: str, params: dict):  # type: ignore[return]
        if action == "gerar_processo":
            return await self._action_gerar_processo(client, params)
        if action == "incluir_documento":
            return await self._action_incluir_documento(client, params)
        if action == "enviar_processo":
            return await self._action_enviar_processo(client, params)
        raise ValueError(f"Unknown action: {action!r}")

    # ── Action handlers ───────────────────────────────────────────────────

    async def _action_gerar_processo(
        self, client: SEIClient, params: dict
    ) -> dict:
        id_tipo = params.get("id_tipo_procedimento")
        especificacao = params.get("especificacao")
        if not id_tipo:
            raise ValueError(
                "'id_tipo_procedimento' is required for action='gerar_processo'."
            )
        if not especificacao:
            raise ValueError("'especificacao' is required for action='gerar_processo'.")
        return await client.gerar_procedimento(
            id_tipo_procedimento=id_tipo,
            especificacao=especificacao,
            nivel_acesso=params.get("nivel_acesso", "0"),
            assuntos=params.get("assuntos"),
            interessados=params.get("interessados"),
            observacao=params.get("observacao"),
            id_hipotese_legal=params.get("id_hipotese_legal"),
            id_unidade=params.get("id_unidade"),
        )

    async def _action_incluir_documento(
        self, client: SEIClient, params: dict
    ) -> dict:
        protocolo_proc = params.get("protocolo_procedimento")
        id_serie = params.get("id_serie")
        if not protocolo_proc:
            raise ValueError(
                "'protocolo_procedimento' is required for action='incluir_documento'."
            )
        if not id_serie:
            raise ValueError("'id_serie' is required for action='incluir_documento'.")
        return await client.incluir_documento(
            protocolo_procedimento=protocolo_proc,
            id_serie=id_serie,
            tipo=params.get("tipo", "G"),
            descricao=params.get("descricao"),
            conteudo_html=params.get("conteudo"),
            nivel_acesso=params.get("nivel_acesso", "0"),
            id_unidade=params.get("id_unidade"),
        )

    async def _action_enviar_processo(
        self, client: SEIClient, params: dict
    ) -> str:
        protocolo = params.get("protocolo")
        unidades = params.get("unidades_destino")
        if not protocolo:
            raise ValueError("'protocolo' is required for action='enviar_processo'.")
        if not unidades:
            raise ValueError(
                "'unidades_destino' (non-empty list) is required for action='enviar_processo'."
            )
        return await client.enviar_processo(
            protocolo=protocolo,
            unidades_destino=list(unidades),
            sin_manter_aberto=params.get("sin_manter_aberto", "N"),
            sin_enviar_email=params.get("sin_enviar_email", "N"),
            id_unidade=params.get("id_unidade"),
        )
