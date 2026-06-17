"""Live read-only tests for the ``sei_pen_read`` tool.

Every test invokes ``SeiPenReadTool.execute`` directly against the configured
SEI installation. They are marked ``sei_live`` and skip when the ``SEI_*``
connection env vars are absent (see ``conftest.py``).

Run with::

    source limbo/sei.sh
    pytest custom_code/tests/sei_pen/test_sei_read.py -m sei_live -v
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from src.shared.security.context import AgentContext
from custom_code.tools.sei_pen.sei_read import SeiPenReadTool

from .conftest import DiscoveredIds

pytestmark = [
    pytest.mark.sei_live,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _run(
    tool: SeiPenReadTool,
    context: AgentContext,
    config: dict,
    params: dict,
):
    return await tool.execute(
        agent_context=context, params=params, config=config, state={}
    )


def _assert_success(result, operation: str) -> dict:
    """Assert a ToolResult succeeded and return its data payload."""
    if result.status != "success":
        err = result.error or {}
        pytest.fail(
            f"operation '{operation}' failed: "
            f"code={err.get('code')} message={err.get('message')}"
        )
    return result.data or {}


# ── Operations that require no input params ──────────────────────────────────

# Operations known to fail on some installations due to WSSEI server-side bugs
# (not a tool defect). They are marked ``xfail`` so the suite stays green while
# still flagging if the upstream ever starts working again.
_SERVER_BUG_OPS = {
    "processo.listar_meus_acompanhamentos": (
        "WSSEI server-side SQL bug: Unknown column "
        "'acompanhamento.id_usuario_gerador'"
    ),
    "modelo_documento.listar": (
        "WSSEI server-side HTTP 500 in ProtocoloModeloRN->listarModelosUnidade"
    ),
}

PARAMETERLESS_OPS: list[dict[str, Any]] = [
    {"operation": "orgao.listar"},
    {"operation": "unidade.pesquisar", "limit": 5},
    {"operation": "unidade.pesquisar_outras", "limit": 5},
    {"operation": "unidade.pesquisar_texto_padrao", "limit": 5},
    {"operation": "processo.pesquisar_assunto", "limit": 5},
    {"operation": "processo.listar", "limit": 5},
    {"operation": "processo.listar_meus_acompanhamentos", "limit": 5},
    {"operation": "processo.pesquisar_geral", "limit": 5},
    {"operation": "grupo_acompanhamento.listar", "limit": 5},
    {"operation": "modelo_documento.listar_grupo", "limit": 5},
    {"operation": "modelo_documento.listar", "limit": 5},
    {"operation": "acompanhamento_especial.listar", "limit": 5},
]


def _parameterless_param(spec: dict[str, Any]):
    """Wrap a parameterless op, applying ``xfail`` for known server bugs."""
    reason = _SERVER_BUG_OPS.get(spec["operation"])
    marks = pytest.mark.xfail(reason=reason, strict=False) if reason else ()
    return pytest.param(spec, marks=marks, id=spec["operation"])


@pytest.mark.parametrize(
    "params", [_parameterless_param(p) for p in PARAMETERLESS_OPS]
)
async def test_read_parameterless(
    read_tool: SeiPenReadTool,
    agent_context: AgentContext,
    sei_config: dict,
    params: dict,
):
    result = await _run(read_tool, agent_context, sei_config, params)
    _assert_success(result, params["operation"])


# ── Operations needing IDs discovered at runtime ─────────────────────────────


async def test_read_processo_consultar(
    read_tool, agent_context, sei_config, discovered_ids: DiscoveredIds
):
    if not discovered_ids.protocolo:
        pytest.skip("no process protocol discovered")
    result = await _run(
        read_tool,
        agent_context,
        sei_config,
        {"operation": "processo.consultar", "protocolo": discovered_ids.protocolo},
    )
    _assert_success(result, "processo.consultar")


async def test_read_processo_consultar_atribuicao(
    read_tool, agent_context, sei_config, discovered_ids: DiscoveredIds
):
    if not discovered_ids.protocolo:
        pytest.skip("no process protocol discovered")
    result = await _run(
        read_tool,
        agent_context,
        sei_config,
        {
            "operation": "processo.consultar_atribuicao",
            "protocolo": discovered_ids.protocolo,
        },
    )
    _assert_success(result, "processo.consultar_atribuicao")


async def test_read_processo_consultar_acompanhamento(
    read_tool, agent_context, sei_config, discovered_ids: DiscoveredIds
):
    if not discovered_ids.protocolo:
        pytest.skip("no process protocol discovered")
    result = await _run(
        read_tool,
        agent_context,
        sei_config,
        {
            "operation": "processo.consultar_acompanhamento",
            "protocolo": discovered_ids.protocolo,
        },
    )
    _assert_success(result, "processo.consultar_acompanhamento")


async def test_read_processo_listar_acompanhamentos(
    read_tool, agent_context, sei_config, discovered_ids: DiscoveredIds
):
    if not discovered_ids.grupo:
        pytest.skip("no tracking group discovered")
    result = await _run(
        read_tool,
        agent_context,
        sei_config,
        {
            "operation": "processo.listar_acompanhamentos",
            "grupo": discovered_ids.grupo,
            "limit": 5,
        },
    )
    _assert_success(result, "processo.listar_acompanhamentos")


async def test_read_documento_listar_em_processo(
    read_tool, agent_context, sei_config, discovered_ids: DiscoveredIds
):
    if not discovered_ids.procedimento:
        pytest.skip("no process internal id discovered")
    result = await _run(
        read_tool,
        agent_context,
        sei_config,
        {
            "operation": "documento.listar_em_processo",
            "procedimento": discovered_ids.procedimento,
        },
    )
    _assert_success(result, "documento.listar_em_processo")


async def test_read_documento_consultar_interno(
    read_tool, agent_context, sei_config, discovered_ids: DiscoveredIds
):
    if not discovered_ids.protocolo:
        pytest.skip("no protocol discovered")
    result = await _run(
        read_tool,
        agent_context,
        sei_config,
        {
            "operation": "documento.consultar_interno",
            "protocolo": discovered_ids.protocolo,
        },
    )
    _assert_success(result, "documento.consultar_interno")


async def test_read_documento_visualizar(
    read_tool, agent_context, sei_config, discovered_ids: DiscoveredIds
):
    if not discovered_ids.documento:
        pytest.skip("no document internal id discovered")
    result = await _run(
        read_tool,
        agent_context,
        sei_config,
        {"operation": "documento.visualizar", "documento": discovered_ids.documento},
    )
    _assert_success(result, "documento.visualizar")


async def test_read_usuario_pesquisar(
    read_tool, agent_context, sei_config
):
    palavrachave = os.getenv("SEI_TEST_PALAVRACHAVE") or os.getenv("SEI_USERNAME") or ""
    if not palavrachave:
        pytest.skip("no SEI_TEST_PALAVRACHAVE / SEI_USERNAME for user search")
    result = await _run(
        read_tool,
        agent_context,
        sei_config,
        {"operation": "usuario.pesquisar", "palavrachave": palavrachave},
    )
    _assert_success(result, "usuario.pesquisar")


async def test_read_usuario_listar_unidades(
    read_tool, agent_context, sei_config
):
    usuario = os.getenv("SEI_USERNAME") or ""
    if not usuario:
        pytest.skip("no SEI_USERNAME for unit listing")
    result = await _run(
        read_tool,
        agent_context,
        sei_config,
        {"operation": "usuario.listar_unidades", "usuario": usuario},
    )
    _assert_success(result, "usuario.listar_unidades")


@pytest.mark.parametrize("nivel_acesso", [0, 1, 2], ids=["publico", "restrito", "sigiloso"])
async def test_read_hipotese_legal_pesquisar(
    read_tool, agent_context, sei_config, nivel_acesso: int
):
    result = await _run(
        read_tool,
        agent_context,
        sei_config,
        {
            "operation": "hipotese_legal.pesquisar",
            "nivelAcesso": nivel_acesso,
            "limit": 5,
        },
    )
    _assert_success(result, "hipotese_legal.pesquisar")

