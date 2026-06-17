"""Live write tests for the ``sei_pen_write`` tool.

These tests create **permanent** artifacts in the target SEI installation —
the WSSEI API exposes no delete operation, so created processes/documents
cannot be cleaned up automatically. They are therefore double-gated:

* marker ``sei_write`` (and ``sei_live``), and
* ``SEI_ALLOW_WRITE=1`` in the environment.

Calling ``tool.execute(...)`` directly bypasses the human-in-the-loop approval
that ``sei_pen_write`` normally requires (approval is enforced by the
orchestration layer, not by ``execute``), which is exactly what we want here.

The tests are chained through session-scoped fixtures:

    processo.criar ──► processo.alterar
          │
          └─► documento.cadastrar_interno ──► documento.alterar_interno
                                          └─► documento.dar_ciencia

Run with::

    source limbo/sei.sh
    SEI_ALLOW_WRITE=1 pytest custom_code/tests/sei_pen/test_sei_write.py \
        -m "sei_live or sei_write" -v
"""

from __future__ import annotations

import os
import time
from typing import Optional

import pytest
import pytest_asyncio

from src.shared.security.context import AgentContext
from custom_code.tools.sei_pen.sei_read import SeiPenReadTool
from custom_code.tools.sei_pen.sei_write import SeiPenWriteTool

from .conftest import TEST_ID_SERIE, first_item, pick

pytestmark = [
    pytest.mark.sei_live,
    pytest.mark.sei_write,
    pytest.mark.asyncio(loop_scope="session"),
]


# Marker to make test-created artifacts easy to recognise / clean up manually.
TEST_TAG = "TESTE-GSAGE"


@pytest.fixture(scope="session", autouse=True)
def _require_write_optin() -> None:
    """Skip the entire write suite unless explicitly enabled."""
    if os.getenv("SEI_ALLOW_WRITE") != "1":
        pytest.skip(
            "write tests create permanent SEI artifacts; set SEI_ALLOW_WRITE=1 "
            "to enable."
        )


async def _run(
    tool: SeiPenWriteTool,
    context: AgentContext,
    config: dict,
    params: dict,
):
    return await tool.execute(
        agent_context=context, params=params, config=config, state={}
    )


def _assert_success(result, operation: str) -> dict:
    if result.status != "success":
        err = result.error or {}
        pytest.fail(
            f"operation '{operation}' failed: "
            f"code={err.get('code')} message={err.get('message')}"
        )
    return result.data or {}


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def created_process(
    write_tool: SeiPenWriteTool,
    read_tool: SeiPenReadTool,
    agent_context: AgentContext,
    sei_config: dict,
) -> dict:
    """Create a process once and share it across the write tests."""
    tipo_processo = os.getenv("SEI_TEST_TIPO_PROCESSO") or ""
    if not tipo_processo:
        pytest.skip("SEI_TEST_TIPO_PROCESSO is required to create a process")

    nivel_acesso = int(os.getenv("SEI_TEST_NIVEL_ACESSO") or 0)

    # For non-public processes, SEI requires a legal hypothesis.
    hipotese_legal = os.getenv("SEI_TEST_HIPOTESE_LEGAL") or ""
    if nivel_acesso > 0 and not hipotese_legal:
        pytest.skip(
            "SEI_TEST_HIPOTESE_LEGAL is required when SEI_TEST_NIVEL_ACESSO > 0"
        )

    # Secrecy degree is only meaningful for sigiloso (nivelAcesso=2).
    grau_sigilo = os.getenv("SEI_TEST_GRAU_SIGILO") or ""

    # Some SEI installations require at least one subject (assunto) to create
    # a process. Discover a valid subject for the chosen process type.
    assuntos = os.getenv("SEI_TEST_ASSUNTO")
    if not assuntos:
        subject_res = await read_tool.execute(
            agent_context=agent_context,
            params={
                "operation": "processo.assunto_sugestao",
                "tipoProcedimento": tipo_processo,
                "limit": 5,
            },
            config=sei_config,
            state={},
        )
        if subject_res and subject_res.status == "success":
            rec = first_item((subject_res.data or {}).get("result"))
            assuntos = pick(rec, "idAssunto", "id")
        if not assuntos:
            pytest.skip(
                f"no subject discovered for process type {tipo_processo}; "
                "set SEI_TEST_ASSUNTO or check the SEI installation."
            )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    params: dict[str, Any] = {
        "operation": "processo.criar",
        "tipoProcesso": tipo_processo,
        "nivelAcesso": nivel_acesso,
        "assuntos": assuntos,
        "especificacao": f"{TEST_TAG} processo {stamp}",
        "observacoes": f"{TEST_TAG} automated write test {stamp}",
    }
    if nivel_acesso > 0:
        params["hipoteseLegal"] = hipotese_legal
        params["grauSigilo"] = grau_sigilo
    result = await _run(write_tool, agent_context, sei_config, params)
    data = _assert_success(result, "processo.criar")

    payload = data.get("result")
    protocolo = pick(
        payload if isinstance(payload, dict) else None,
        "protocoloProcedimentoFormatado",
        "protocolo",
    )
    procedimento = pick(
        payload if isinstance(payload, dict) else None,
        "idProcedimento",
        "idProtocolo",
        "id",
    )
    print(
        f"\n[sei_write] created process: protocolo={protocolo} "
        f"procedimento={procedimento} tipo={tipo_processo}"
    )
    return {
        "protocolo": protocolo,
        "procedimento": procedimento,
        "tipoProcesso": tipo_processo,
        "grauSigilo": grau_sigilo,
    }


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def created_document(
    write_tool: SeiPenWriteTool,
    agent_context: AgentContext,
    sei_config: dict,
    created_process: dict,
) -> dict:
    """Create an internal document inside the test process."""
    procedimento = created_process.get("procedimento")
    if not procedimento:
        pytest.skip("created process has no internal id; cannot create document")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    params = {
        "operation": "documento.cadastrar_interno",
        "procedimento": procedimento,
        "idSerie": TEST_ID_SERIE,
        "observacao": f"{TEST_TAG} documento {stamp}",
        "nivelAcesso": 0,
        "descricao": f"{TEST_TAG} automated write test {stamp}",
    }
    result = await _run(write_tool, agent_context, sei_config, params)
    data = _assert_success(result, "documento.cadastrar_interno")

    payload = data.get("result")
    documento = pick(
        payload if isinstance(payload, dict) else None,
        "idDocumento",
        "idProtocolo",
        "documento",
        "id",
    )
    print(f"\n[sei_write] created document: documento={documento} serie={TEST_ID_SERIE}")
    return {"documento": documento}


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_processo_criar(created_process: dict):
    # The fixture performs the creation and asserts success; here we assert the
    # process is usable downstream.
    assert created_process.get("protocolo") or created_process.get("procedimento"), (
        "processo.criar returned neither a protocol nor an internal id"
    )


async def test_processo_alterar(
    write_tool: SeiPenWriteTool,
    agent_context: AgentContext,
    sei_config: dict,
    created_process: dict,
):
    protocolo = created_process.get("protocolo") or created_process.get("procedimento")
    if not protocolo:
        pytest.skip("no protocol available to update")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    params = {
        "operation": "processo.alterar",
        "protocolo": protocolo,
        "idTipoProcesso": created_process["tipoProcesso"],
        "nivelAcesso": 0,
        "grauSigilo": created_process["grauSigilo"],
        "especificacao": f"{TEST_TAG} processo alterado {stamp}",
    }
    result = await _run(write_tool, agent_context, sei_config, params)
    _assert_success(result, "processo.alterar")


async def test_documento_cadastrar_interno(created_document: dict):
    assert created_document.get("documento"), (
        "documento.cadastrar_interno returned no document id"
    )


async def test_documento_alterar_interno(
    write_tool: SeiPenWriteTool,
    agent_context: AgentContext,
    sei_config: dict,
    created_document: dict,
):
    documento: Optional[str] = created_document.get("documento")
    if not documento:
        pytest.skip("no document id available to update")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    params = {
        "operation": "documento.alterar_interno",
        "documento": documento,
        "observacao": f"{TEST_TAG} documento alterado {stamp}",
        "nivelAcesso": 0,
        "descricao": f"{TEST_TAG} updated {stamp}",
    }
    result = await _run(write_tool, agent_context, sei_config, params)
    _assert_success(result, "documento.alterar_interno")


async def test_documento_dar_ciencia(
    write_tool: SeiPenWriteTool,
    agent_context: AgentContext,
    sei_config: dict,
    created_document: dict,
):
    documento: Optional[str] = created_document.get("documento")
    if not documento:
        pytest.skip("no document id available to acknowledge")

    params = {"operation": "documento.dar_ciencia", "documento": documento}
    result = await _run(write_tool, agent_context, sei_config, params)
    _assert_success(result, "documento.dar_ciencia")
