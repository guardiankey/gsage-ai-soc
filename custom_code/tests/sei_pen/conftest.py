"""Fixtures for live SEI-PEN tool tests.

Credentials and connection details are read **only** from environment
variables (no secrets in code). Populate them by sourcing the local,
git-ignored ``limbo/sei.sh`` before running:

    source limbo/sei.sh
    pytest custom_code/tests/sei_pen/ -m sei_live -v

Recognised environment variables
--------------------------------
Connection (required for any live test):
    SEI_BASE_URL    Custom WSSEI v2 base URL (overrides ambiente).
    SEI_ORGAO_ID    SEI organ/agency numeric ID (e.g. "0").
    SEI_USERNAME    SEI login/sigla.
    SEI_PASSWORD    SEI password.
Optional connection:
    SEI_AMBIENTE    Environment preset (used only when SEI_BASE_URL is unset).
    SEI_UNIDADE_ID  Default unit ID kept as session context.

Write-test overrides (used by test_sei_write.py):
    SEI_ALLOW_WRITE         Must equal "1" to enable write tests.
    SEI_TEST_TIPO_PROCESSO  Process type ID for processo.criar.
    SEI_TEST_HIPOTESE_LEGAL Legal hypothesis value for processo.criar.
    SEI_TEST_GRAU_SIGILO    Secrecy degree for processo.criar (often "").
    SEI_TEST_ASSUNTO        Subject ID for processo.criar. When omitted, the test
                            tries to discover one via processo.assunto_sugestao.
    SEI_TEST_PALAVRACHAVE   Keyword for usuario.pesquisar (defaults to username).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import pytest
import pytest_asyncio

from src.shared.security.context import AgentContext, RequestSource
from custom_code.tools.sei_pen.sei_read import SeiPenReadTool
from custom_code.tools.sei_pen.sei_write import SeiPenWriteTool


# Document type (série) locked for tests until a discovery step exists in the
# UI/tool. The system screen "documento_escolher_tipo" lists types with their
# séries; 306 is the fixed one used here.
TEST_ID_SERIE: str = os.getenv("SEI_TEST_ID_SERIE") or "306"


@dataclass(frozen=True)
class SeiEnv:
    """Resolved SEI-PEN connection settings from the environment."""

    base_url: Optional[str]
    ambiente: Optional[str]
    orgao_id: str
    unidade_id: Optional[str]
    username: str
    password: str


@pytest.fixture(scope="session")
def sei_env() -> SeiEnv:
    """Resolve SEI-PEN connection settings or skip the whole live suite."""
    base_url = os.getenv("SEI_BASE_URL") or None
    ambiente = os.getenv("SEI_AMBIENTE") or None
    orgao_id = (os.getenv("SEI_ORGAO_ID") or "").strip()
    unidade_id = (os.getenv("SEI_UNIDADE_ID") or "").strip() or None
    username = (os.getenv("SEI_USERNAME") or "").strip()
    password = os.getenv("SEI_PASSWORD") or ""

    missing: list[str] = []
    if not base_url and not ambiente:
        missing.append("SEI_BASE_URL (or SEI_AMBIENTE)")
    if not orgao_id:
        missing.append("SEI_ORGAO_ID")
    if not username:
        missing.append("SEI_USERNAME")
    if not password:
        missing.append("SEI_PASSWORD")

    if missing:
        pytest.skip(
            "SEI-PEN live tests require environment variables: "
            + ", ".join(missing)
            + ". Run `source limbo/sei.sh` first."
        )

    return SeiEnv(
        base_url=base_url,
        ambiente=ambiente,
        orgao_id=orgao_id,
        unidade_id=unidade_id,
        username=username,
        password=password,
    )


@pytest.fixture(scope="session")
def sei_config(sei_env: SeiEnv) -> dict[str, Any]:
    """Tool config block (normally stored encrypted in the DB)."""
    config: dict[str, Any] = {}
    if sei_env.base_url:
        config["base_url"] = sei_env.base_url
    elif sei_env.ambiente:
        config["ambiente"] = sei_env.ambiente
    return config


@pytest.fixture(scope="session")
def agent_context(sei_env: SeiEnv) -> AgentContext:
    """Minimal AgentContext carrying SEI credentials in keychain runtime shape.

    The tool reads credentials from
    ``agent_context.user_credentials["sei_pen"]`` with ``username``,
    ``password`` and an ``extra_fields`` dict holding ``orgao_id`` /
    ``unidade_id``.
    """
    return AgentContext(
        org_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        group_ids=[],
        permissions=["sei:read", "sei:write"],
        request_id=uuid.uuid4(),
        source=RequestSource.CLI,
        user_credentials={
            "sei_pen": {
                "username": sei_env.username,
                "password": sei_env.password,
                "extra_fields": {
                    "orgao_id": sei_env.orgao_id,
                    "unidade_id": sei_env.unidade_id or "",
                },
            }
        },
    )


@pytest.fixture(scope="session")
def read_tool() -> SeiPenReadTool:
    return SeiPenReadTool()


@pytest.fixture(scope="session")
def write_tool() -> SeiPenWriteTool:
    return SeiPenWriteTool()


def first_item(payload: Any) -> Optional[dict]:
    """Return the first dict-like record from a SEI ``result`` payload.

    SEI list endpoints return either a list directly or a dict wrapping the
    list under a single key. This normalises both shapes.
    """
    if isinstance(payload, list):
        return payload[0] if payload else None
    if isinstance(payload, dict):
        # A detail payload is itself the record.
        for value in payload.values():
            if isinstance(value, list) and value:
                return value[0] if isinstance(value[0], dict) else None
        return payload
    return None


def pick(record: Optional[dict], *keys: str) -> Optional[str]:
    """Return the first present, non-empty value among *keys* in *record*.

    Matching is case-insensitive because the SEI WSSEI module is inconsistent:
    e.g. ``processo.criar`` returns ``IdProcedimento`` / ``ProtocoloFormatado``
    while other endpoints return ``idProcedimento`` / ``protocoloProcedimentoFormatado``.
    """
    if not isinstance(record, dict):
        return None
    lowered = {str(k).lower(): v for k, v in record.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, "", []):
            return str(value)
    return None


@dataclass
class DiscoveredIds:
    """IDs harvested from read operations, reused by dependent read tests."""

    grupo: Optional[str] = None
    protocolo: Optional[str] = None
    procedimento: Optional[str] = None
    documento: Optional[str] = None


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def discovered_ids(
    read_tool: SeiPenReadTool,
    agent_context: AgentContext,
    sei_config: dict,
) -> DiscoveredIds:
    """Run a few read operations once to harvest IDs for dependent tests.

    Any failure is tolerated — dependent tests skip when an ID is missing.
    """
    ids = DiscoveredIds()

    async def run(params: dict):
        try:
            return await read_tool.execute(
                agent_context=agent_context,
                params=params,
                config=sei_config,
                state={},
            )
        except Exception:  # pragma: no cover - discovery is best-effort
            return None

    # Tracking group
    res = await run({"operation": "grupo_acompanhamento.listar", "limit": 5})
    if res and res.status == "success":
        ids.grupo = pick(first_item((res.data or {}).get("result")), "idGrupo", "id")

    # A process the current user can see (protocol + internal id).
    res = await run({"operation": "processo.listar", "limit": 5})
    if res and res.status == "success":
        rec = first_item((res.data or {}).get("result"))
        ids.protocolo = pick(rec, "protocoloProcedimentoFormatado", "protocolo")
        ids.procedimento = pick(rec, "idProcedimento", "idProtocolo", "id")

    # A document inside that process.
    if ids.procedimento:
        res = await run(
            {"operation": "documento.listar_em_processo", "procedimento": ids.procedimento}
        )
        if res and res.status == "success":
            rec = first_item((res.data or {}).get("result"))
            ids.documento = pick(rec, "idDocumento", "idProtocolo", "documento", "id")

    return ids
