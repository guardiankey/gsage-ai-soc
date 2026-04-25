"""gSage AI — SEI Read tool.

Queries the SEI (Sistema Eletrônico de Informações) SOAP webservice for
read-only information: processes, documents, units, series, process types,
users, and history entries.

Required permission: ``sei:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

from custom_code.tools.seiws._client import SEIClient, SEIError

log = logging.getLogger(__name__)


class SeiReadTool(BaseTool):
    """Query the SEI webservice for read-only information.

    **Available actions:**

    | Action                      | Description                                          |
    |-----------------------------|------------------------------------------------------|
    | ``consultar_processo``      | Retrieve full details of a process by protocol       |
    | ``consultar_documento``     | Retrieve document details by formatted protocol      |
    | ``listar_unidades``         | List all organizational units visible to the system  |
    | ``listar_series``           | List document series (tipos de série) for a unit     |
    | ``listar_tipos_procedimento`` | List process types available for a unit            |
    | ``listar_usuarios``         | List users of a given unit                           |
    | ``listar_andamentos``       | List history entries for a process                   |

    **Action-specific parameters:**

    *consultar_processo*: ``protocolo`` (required)

    *consultar_documento*: ``protocolo_documento`` (required)

    *listar_unidades*: ``id_tipo_procedimento`` (optional filter), ``id_serie`` (optional filter)

    *listar_series*: ``id_unidade`` (optional override), ``id_tipo_procedimento`` (optional filter)

    *listar_tipos_procedimento*: ``id_unidade`` (optional override), ``id_serie`` (optional filter)

    *listar_usuarios*: ``id_unidade`` (optional override)

    *listar_andamentos*: ``protocolo`` (required), ``id_unidade`` (optional override)

    Permission: ``sei:read``
    """

    name: ClassVar[str] = "sei_read"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Read documents, processes, and metadata from the SEI document management system"
    category: ClassVar[str] = "document"
    available: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["sei:read"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "consultar_processo",
                    "consultar_documento",
                    "listar_unidades",
                    "listar_series",
                    "listar_tipos_procedimento",
                    "listar_usuarios",
                    "listar_andamentos",
                ],
                "description": "SEI read operation to perform.",
            },
            "protocolo": {
                "type": "string",
                "description": (
                    "SEI process protocol number (e.g. '35014.000001/2020-31'). "
                    "Required for actions: consultar_processo, listar_andamentos."
                ),
            },
            "protocolo_documento": {
                "type": "string",
                "description": (
                    "SEI document formatted protocol number. "
                    "Required for action: consultar_documento."
                ),
            },
            "id_unidade": {
                "type": "string",
                "description": (
                    "Unit ID override for this call. "
                    "If omitted, the default unit from config is used."
                ),
            },
            "id_tipo_procedimento": {
                "type": "string",
                "description": (
                    "Process type ID for filtering. "
                    "Used with: listar_unidades, listar_series."
                ),
            },
            "id_serie": {
                "type": "string",
                "description": (
                    "Series ID for filtering. "
                    "Used with: listar_unidades, listar_tipos_procedimento."
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
                "description": "SEI WSDL URL (e.g. https://sei.example.gov.br/sei/ws/SeiWS.php?wsdl).",
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
            "sei_read: action=%s params=%r",
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
            log.exception("sei_read: unexpected error in action=%s", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        log.debug("sei_read: action=%s OK in %dms", action, elapsed)
        return self._success(
            data={"action": action, "result": result},
            execution_time_ms=elapsed,
        )

    # ── Dispatcher ────────────────────────────────────────────────────────

    async def _dispatch(self, client: SEIClient, action: str, params: dict):  # type: ignore[return]
        if action == "consultar_processo":
            return await self._action_consultar_processo(client, params)
        if action == "consultar_documento":
            return await self._action_consultar_documento(client, params)
        if action == "listar_unidades":
            return await self._action_listar_unidades(client, params)
        if action == "listar_series":
            return await self._action_listar_series(client, params)
        if action == "listar_tipos_procedimento":
            return await self._action_listar_tipos_procedimento(client, params)
        if action == "listar_usuarios":
            return await self._action_listar_usuarios(client, params)
        if action == "listar_andamentos":
            return await self._action_listar_andamentos(client, params)
        raise ValueError(f"Unknown action: {action!r}")

    # ── Action handlers ───────────────────────────────────────────────────

    async def _action_consultar_processo(
        self, client: SEIClient, params: dict
    ) -> dict:
        protocolo = params.get("protocolo")
        if not protocolo:
            raise ValueError("'protocolo' is required for action='consultar_processo'.")
        return await client.consultar_procedimento(
            protocolo=protocolo,
            id_unidade=params.get("id_unidade"),
        )

    async def _action_consultar_documento(
        self, client: SEIClient, params: dict
    ) -> dict:
        protocolo_doc = params.get("protocolo_documento")
        if not protocolo_doc:
            raise ValueError(
                "'protocolo_documento' is required for action='consultar_documento'."
            )
        return await client.consultar_documento(
            protocolo_documento=protocolo_doc,
            id_unidade=params.get("id_unidade"),
        )

    async def _action_listar_unidades(
        self, client: SEIClient, params: dict
    ) -> list:
        return await client.listar_unidades(
            id_tipo_procedimento=params.get("id_tipo_procedimento"),
            id_serie=params.get("id_serie"),
        )

    async def _action_listar_series(
        self, client: SEIClient, params: dict
    ) -> list:
        return await client.listar_series(
            id_unidade=params.get("id_unidade"),
            id_tipo_procedimento=params.get("id_tipo_procedimento"),
        )

    async def _action_listar_tipos_procedimento(
        self, client: SEIClient, params: dict
    ) -> list:
        return await client.listar_tipos_procedimento(
            id_unidade=params.get("id_unidade"),
            id_serie=params.get("id_serie"),
        )

    async def _action_listar_usuarios(
        self, client: SEIClient, params: dict
    ) -> list:
        return await client.listar_usuarios(
            id_unidade=params.get("id_unidade"),
        )

    async def _action_listar_andamentos(
        self, client: SEIClient, params: dict
    ) -> list:
        protocolo = params.get("protocolo")
        if not protocolo:
            raise ValueError("'protocolo' is required for action='listar_andamentos'.")
        return await client.listar_andamentos(
            protocolo=protocolo,
            id_unidade=params.get("id_unidade"),
        )
