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
from typing import Any, Optional

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
    sei_env,
) -> dict:
    """Create a process once and share it across the write tests."""
    if not sei_env.unidade_id:
        pytest.skip(
            "SEI_UNIDADE_ID is required for write operations — SEI rejects "
            "writes without a unit header. Set it in limbo/sei.sh."
        )

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
    print(
        f"\n[sei_write] processo.criar params: tipo={tipo_processo} "
        f"nivel={nivel_acesso} assuntos={assuntos!r} "
        f"unidade_id={sei_env.unidade_id!r} orgao_id={sei_env.orgao_id!r}"
    )
    result = await _run(write_tool, agent_context, sei_config, params)
    data = _assert_success(result, "processo.criar")

    payload = data.get("result")
    # processo.criar returns {"IdProcedimento", "ProtocoloFormatado"} (capitalized);
    # ``pick`` is case-insensitive so the keys below cover both old and new spellings.
    protocolo = pick(
        payload if isinstance(payload, dict) else None,
        "ProtocoloFormatado",
        "protocoloProcedimentoFormatado",
        "protocolo",
    )
    procedimento = pick(
        payload if isinstance(payload, dict) else None,
        "IdProcedimento",
        "idProcedimento",
        "idProtocolo",
        "id",
    )
    print(
        f"\n[sei_write] created process: protocolo={protocolo} "
        f"procedimento={procedimento} tipo={tipo_processo} "
        f"raw_payload_keys={sorted(payload.keys()) if isinstance(payload, dict) else None}"
    )
    return {
        "protocolo": protocolo,
        "procedimento": procedimento,
        "tipoProcesso": tipo_processo,
        "grauSigilo": grau_sigilo,
        "assuntos": assuntos,
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
    # ``processo.alterar`` resolves the path as /processo/{protocolo}/alterar.
    # The *formatted* protocol (e.g. ``Assefaz.000005/2026-80``) contains '/'
    # and breaks the route; the internal numeric id (``procedimento``) is the
    # safe identifier here.
    protocolo = created_process.get("procedimento") or created_process.get("protocolo")
    if not protocolo:
        pytest.skip("no protocol available to update")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    params = {
        "operation": "processo.alterar",
        "protocolo": protocolo,
        "idTipoProcesso": created_process["tipoProcesso"],
        "nivelAcesso": 0,
        "grauSigilo": created_process["grauSigilo"],
        # SEI's alterar endpoint validates ``assuntos`` again and refuses
        # the update with "Nenhum assunto informado" if it is missing,
        # even when the process already has subjects.
        "assuntos": created_process["assuntos"],
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


# ── Document content (sections) ──────────────────────────────────────────────


async def test_documento_secao_listar(
    read_tool: SeiPenReadTool,
    agent_context: AgentContext,
    sei_config: dict,
    created_document: dict,
):
    """Low-level read of the document's editable sections."""
    documento: Optional[str] = created_document.get("documento")
    if not documento:
        pytest.skip("no document id available to list sections")

    result = await read_tool.execute(
        agent_context=agent_context,
        params={"operation": "documento.secao_listar", "id": documento},
        config=sei_config,
        state={},
    )
    data = _assert_success(result, "documento.secao_listar")
    payload = data.get("result") or {}
    assert isinstance(payload, dict), f"expected dict, got {type(payload).__name__}"
    secoes = payload.get("secoes") or []
    print(
        f"\n[sei_write] secao_listar: doc={documento} "
        f"versao={payload.get('ultimaVersaoDocumento')} count={len(secoes)}"
    )
    assert secoes, "newly-created document should expose at least one section"


async def test_documento_ver_completo(
    read_tool: SeiPenReadTool,
    agent_context: AgentContext,
    sei_config: dict,
    created_document: dict,
):
    """High-level read helper — metadata + HTML + sections in one call."""
    documento: Optional[str] = created_document.get("documento")
    if not documento:
        pytest.skip("no document id available")

    result = await read_tool.execute(
        agent_context=agent_context,
        params={"operation": "documento.ver_completo", "documento": documento},
        config=sei_config,
        state={},
    )
    data = _assert_success(result, "documento.ver_completo")
    payload = data.get("result") or {}
    assert payload.get("documento") == documento
    # Each branch may individually fail with a partial-error dict; we only
    # require that the helper returned them (and not that all succeeded).
    assert "metadados" in payload
    assert "html_renderizado" in payload
    assert "secoes" in payload
    print(
        f"\n[sei_write] ver_completo: doc={documento} "
        f"versao={payload.get('versao')} secoes={len(payload.get('secoes') or [])}"
    )


async def test_documento_atualizar_conteudo_quick(
    write_tool: SeiPenWriteTool,
    agent_context: AgentContext,
    sei_config: dict,
    created_document: dict,
):
    """High-level write helper — overwrites the editable principal section."""
    documento: Optional[str] = created_document.get("documento")
    if not documento:
        pytest.skip("no document id available")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    params = {
        "operation": "documento.atualizar_conteudo",
        "documento": documento,
        "conteudo_html": (
            f"<p>{TEST_TAG} quick-mode update {stamp}</p>"
        ),
    }
    result = await _run(write_tool, agent_context, sei_config, params)
    data = _assert_success(result, "documento.atualizar_conteudo")
    payload = data.get("result") or {}
    print(
        f"\n[sei_write] atualizar_conteudo (quick): doc={documento} "
        f"v_antes={payload.get('versaoAnterior')} "
        f"v_depois={payload.get('novaVersao')} "
        f"secoes={payload.get('secoesAtualizadas')}"
    )
    assert payload.get("secoesAtualizadas", 0) >= 1


async def test_documento_atualizar_conteudo_batch(
    read_tool: SeiPenReadTool,
    write_tool: SeiPenWriteTool,
    agent_context: AgentContext,
    sei_config: dict,
    created_document: dict,
):
    """High-level write helper — batch update of every editable section."""
    documento: Optional[str] = created_document.get("documento")
    if not documento:
        pytest.skip("no document id available")

    # Discover the current editable sections through ver_completo so we exercise
    # the matching logic (by id) rather than rewriting the principal blindly.
    read_res = await read_tool.execute(
        agent_context=agent_context,
        params={
            "operation": "documento.ver_completo",
            "documento": documento,
            "incluir_visualizacao": False,
        },
        config=sei_config,
        state={},
    )
    read_data = _assert_success(read_res, "documento.ver_completo")
    secoes_now = (read_data.get("result") or {}).get("secoes") or []
    editable = [s for s in secoes_now if not s.get("somenteLeitura")]
    if not editable:
        pytest.skip("document has no editable sections to update in batch")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    batch = [
        {
            "id": str(s["id"]),
            "conteudo": f"<p>{TEST_TAG} batch #{idx} {stamp}</p>",
        }
        for idx, s in enumerate(editable, start=1)
    ]

    params = {
        "operation": "documento.atualizar_conteudo",
        "documento": documento,
        "secoes": batch,
    }
    result = await _run(write_tool, agent_context, sei_config, params)
    data = _assert_success(result, "documento.atualizar_conteudo")
    payload = data.get("result") or {}
    print(
        f"\n[sei_write] atualizar_conteudo (batch): doc={documento} "
        f"v_antes={payload.get('versaoAnterior')} "
        f"v_depois={payload.get('novaVersao')} "
        f"secoes={payload.get('secoesAtualizadas')}/{len(batch)}"
    )
    assert payload.get("secoesAtualizadas") == len(batch)


@pytest.mark.xfail(
    reason=(
        "SEI requires the document to be signed before 'dar_ciencia' is "
        "accepted. This suite does not sign documents (signing needs a "
        "certificate/PIN), so the server legitimately rejects with 'O "
        "Documento precisa ser assinado.' Re-enable when a signing step "
        "is added to the fixture chain."
    ),
    strict=False,
)
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
