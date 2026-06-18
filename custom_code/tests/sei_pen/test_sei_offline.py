"""Offline unit tests for SEI-PEN pure helpers.

These do not touch the network or require credentials — they exercise the
name-resolution, date math, bucketing, Mermaid rendering and error-hint logic.

Run with::

    pytest custom_code/tests/sei_pen/test_sei_offline.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

import pytest

from custom_code.tools.sei_pen import _dashboard as dash
from custom_code.tools.sei_pen import _helpers as helpers
from custom_code.tools.sei_pen._client import (
    SeiPenError,
    _clean_error_text,
    _is_list_endpoint,
    instructive_hint,
    with_hint,
)
from custom_code.tools.sei_pen._helpers import HelperError

pytestmark = pytest.mark.unit


# ── match_by_name ─────────────────────────────────────────────────────────────


def test_match_by_name_exact():
    records = [
        {"id": "1", "nome": "Ofício"},
        {"id": "2", "nome": "Despacho"},
    ]
    resolved, candidates = helpers.match_by_name(records, "despacho")
    assert resolved == "2"
    assert candidates == []


def test_match_by_name_unique_partial():
    records = [
        {"id": "1", "nome": "Ofício Circular"},
        {"id": "2", "nome": "Despacho"},
    ]
    resolved, candidates = helpers.match_by_name(records, "circular")
    assert resolved == "1"
    assert candidates == []


def test_match_by_name_ambiguous_returns_candidates():
    records = [
        {"id": "1", "nome": "Ofício de Encaminhamento"},
        {"id": "2", "nome": "Ofício de Resposta"},
    ]
    resolved, candidates = helpers.match_by_name(records, "ofício")
    assert resolved is None
    assert {c["id"] for c in candidates} == {"1", "2"}


def test_match_by_name_alternate_id_name_keys():
    records = [{"idSerie": "306", "nomeSerie": "Memorando"}]
    resolved, _ = helpers.match_by_name(records, "memorando")
    assert resolved == "306"


# ── deadline date math ────────────────────────────────────────────────────────


def test_compute_dt_programada_explicit_date():
    assert helpers._compute_dt_programada(dias=None, data="25/12/2025") == "25/12/2025"


def test_compute_dt_programada_days_ahead():
    expected = (datetime.now() + timedelta(days=5)).strftime("%d/%m/%Y")
    assert helpers._compute_dt_programada(dias=5, data=None) == expected


def test_compute_dt_programada_requires_input():
    with pytest.raises(HelperError):
        helpers._compute_dt_programada(dias=None, data=None)


# ── dashboard pure helpers ────────────────────────────────────────────────────


def test_parse_sei_date_formats():
    assert dash._parse_sei_date("01/02/2025") == datetime(2025, 2, 1)
    assert dash._parse_sei_date("01/02/2025 13:45") == datetime(2025, 2, 1, 13, 45)
    assert dash._parse_sei_date("") is None
    assert dash._parse_sei_date("not-a-date") is None


def test_idle_bucket_boundaries():
    assert dash._idle_bucket(0) == dash._BUCKET_FRESH
    assert dash._idle_bucket(6) == dash._BUCKET_FRESH
    assert dash._idle_bucket(7) == dash._BUCKET_MID
    assert dash._idle_bucket(30) == dash._BUCKET_MID
    assert dash._idle_bucket(31) == dash._BUCKET_STALE


def test_pie_skips_zero_and_empty():
    assert dash._pie("t", {}) == ""
    assert dash._pie("t", {"a": 0, "b": 0}) == ""
    out = dash._pie("Title", {"a": 2, "b": 0, "c": 1})
    assert "pie showData" in out
    assert '"a" : 2' in out
    assert '"c" : 1' in out
    assert '"b"' not in out


def test_as_list_normalisation():
    assert dash._as_list([{"x": 1}, "skip", {"y": 2}]) == [{"x": 1}, {"y": 2}]
    assert dash._as_list({"items": [{"a": 1}]}) == [{"a": 1}]
    assert dash._as_list({"a": 1}) == [{"a": 1}]
    assert dash._as_list(None) == []


# ── error hints ───────────────────────────────────────────────────────────────


def test_instructive_hint_acompanhamentos_bug():
    hint = instructive_hint(
        "Unknown column 'acompanhamento.id_usuario_gerador' in 'field list'",
        500,
        "processo.listar_meus_acompanhamentos",
    )
    assert hint and "apenasMeus" in hint


def test_with_hint_appends_when_present():
    msg = "Unknown column 'acompanhamento.id_usuario_gerador'"
    out = with_hint(msg, 500, "processo.listar_meus_acompanhamentos")
    assert msg in out
    assert len(out) > len(msg)


def test_with_hint_noop_when_no_mapping():
    msg = "some unrelated error"
    out = with_hint(msg, None, "processo.consultar")
    assert out == msg


def test_instructive_hint_relacionamentos_404():
    """404 on processo.relacionamentos → friendly 'no relationships' hint."""
    hint = instructive_hint(
        "Page Not Found", 404, "processo.relacionamentos"
    )
    assert hint and "no related process" in hint.lower()


def test_instructive_hint_visualizar_empty():
    """Empty visualizar → hint about using ver_completo."""
    hint = instructive_hint(
        "", None, "documento.visualizar"
    )
    assert hint and "ver_completo" in hint


# ── _clean_error_text ────────────────────────────────────────────────────────


def test_clean_error_text_strips_html_tags():
    html = (
        "<html><head><title>Slim Application Error</title>"
        "<style>body{margin:0;padding:30px;font:12px/1.5 sans-serif}"
        "h1{font-size:48px}</style></head>"
        "<body><h1>Slim Application Error</h1>"
        "<p>The application could not run.</p>"
        "<h2>Details</h2>"
        "<div><strong>Type:</strong> InfraException</div>"
        "</body></html>"
    )
    cleaned = _clean_error_text(html)
    assert "<" not in cleaned
    assert "{" not in cleaned  # CSS stripped
    assert "margin" not in cleaned.lower()  # CSS stripped
    assert "Slim Application Error" in cleaned
    assert "InfraException" in cleaned


def test_clean_error_text_strips_numeric_entities():
    assert "—" not in _clean_error_text("error&#8212;detail")
    assert "—" not in _clean_error_text("error&mdash;detail")


def test_clean_error_text_empty():
    assert _clean_error_text("") == ""


def test_clean_error_text_plain_text_passthrough():
    assert _clean_error_text("Simple error message") == "Simple error message"


def test_clean_error_text_truncates():
    long_text = "x " * 500
    result = _clean_error_text(long_text)
    assert len(result) <= 400


# ── _is_list_endpoint ────────────────────────────────────────────────────────


def test_is_list_endpoint_true():
    assert _is_list_endpoint("/atividade/listar") is True
    assert _is_list_endpoint("/processo/listar") is True
    assert _is_list_endpoint("/processo/pesquisar") is True
    assert _is_list_endpoint("/documento/listar/123") is True
    assert _is_list_endpoint("/documento/tipo/pesquisar") is True


def test_is_list_endpoint_false():
    assert _is_list_endpoint("/processo/123") is False
    assert _is_list_endpoint("/processo/123/relacionamentos") is False
    assert _is_list_endpoint("/documento/interno/consultar/123") is False
    assert _is_list_endpoint("/autenticar") is False


# ── ver_documento_completo (read helper) ──────────────────────────────────────


class _StubClient:
    """In-memory stand-in for ``SeiPenClient``.

    The constructor maps ``(METHOD, path)`` keys to either a canned envelope
    dict or a ``SeiPenError`` to raise. Every call is recorded in ``self.calls``.
    """

    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.calls: list[dict] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        unidade_override: Optional[str] = None,
    ) -> dict:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "data": data,
                "unidade_override": unidade_override,
            }
        )
        key = (method, path)
        if key not in self.routes:
            raise AssertionError(f"Unexpected call: {method} {path}")
        value = self.routes[key]
        if isinstance(value, SeiPenError):
            raise value
        return value


def _envelope(data: Any) -> dict:
    return {"sucesso": True, "mensagem": "", "data": data, "total": None}


@pytest.mark.asyncio
async def test_ver_documento_completo_full_run():
    client = _StubClient(
        {
            ("GET", "/documento/interno/consultar/42"): _envelope(
                {"id": "42", "numero": "0001/2025"}
            ),
            ("GET", "/documento/42/interno/visualizar"): _envelope(
                "<html>body</html>"
            ),
            ("GET", "/documento/secao/listar"): _envelope(
                {
                    "ultimaVersaoDocumento": "3",
                    "secoes": [
                        {
                            "id": "10",
                            "idSecaoModelo": "100",
                            "PrincipalSecaoDocumento": "S",
                            "somenteLeitura": "N",
                            "conteudo": "<p>x</p>",
                        }
                    ],
                }
            ),
        }
    )
    result = await helpers.ver_documento_completo(
        client=client,  # type: ignore[arg-type]
        unidade_override="123",
        documento="42",
    )
    assert result["documento"] == "42"
    assert result["metadados"]["numero"] == "0001/2025"
    assert result["html_renderizado"] == "<html>body</html>"
    assert result["versao"] == "3"
    assert len(result["secoes"]) == 1
    assert result["secoes"][0]["principal"] is True
    assert result["secoes"][0]["somenteLeitura"] is False
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_ver_documento_completo_skip_secoes():
    client = _StubClient(
        {
            ("GET", "/documento/interno/consultar/7"): _envelope({"id": "7"}),
            ("GET", "/documento/7/interno/visualizar"): _envelope("<p/>"),
        }
    )
    result = await helpers.ver_documento_completo(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        documento="7",
        incluir_secoes=False,
    )
    assert "secoes" not in result
    assert "versao" not in result
    assert "html_renderizado" in result
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_ver_documento_completo_partial_failure_surfaces_error():
    client = _StubClient(
        {
            ("GET", "/documento/interno/consultar/9"): _envelope({"id": "9"}),
            ("GET", "/documento/9/interno/visualizar"): SeiPenError(
                "doc has no preview", status_code=None
            ),
            ("GET", "/documento/secao/listar"): _envelope(
                {"ultimaVersaoDocumento": "1", "secoes": []}
            ),
        }
    )
    result = await helpers.ver_documento_completo(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        documento="9",
    )
    assert result["metadados"]["id"] == "9"
    assert isinstance(result["html_renderizado"], dict)
    assert result["html_renderizado"].get("_error") == "doc has no preview"
    assert result["secoes"] == []


@pytest.mark.asyncio
async def test_ver_documento_completo_requires_documento():
    client = _StubClient({})
    with pytest.raises(HelperError):
        await helpers.ver_documento_completo(
            client=client,  # type: ignore[arg-type]
            unidade_override=None,
            documento="",
        )


# ── atualizar_documento_conteudo (write helper) ───────────────────────────────


_CURRENT_SECTIONS = _envelope(
    {
        "ultimaVersaoDocumento": "5",
        "secoes": [
            {
                "id": "10",
                "idSecaoModelo": "100",
                "PrincipalSecaoDocumento": "S",
                "somenteLeitura": "N",
                # dataToUtf8 double-encodes: & → &amp; on every read.
                # The payload sent back must html.unescape this to prevent
                # progressive corruption.
                "conteudo": "&amp;lt;p&amp;gt;old principal&amp;lt;/p&amp;gt;",
            },
            {
                "id": "11",
                "idSecaoModelo": "101",
                "PrincipalSecaoDocumento": "N",
                "somenteLeitura": "N",
                "conteudo": "&amp;lt;p&amp;gt;old extra&amp;lt;/p&amp;gt;",
            },
            {
                "id": "12",
                "idSecaoModelo": "102",
                "PrincipalSecaoDocumento": "N",
                "somenteLeitura": "S",
                "conteudo": "&amp;lt;p&amp;gt;read only&amp;lt;/p&amp;gt;",
            },
        ],
    }
)


@pytest.mark.asyncio
async def test_atualizar_quick_mode_writes_principal():
    client = _StubClient(
        {
            ("GET", "/documento/secao/listar"): _CURRENT_SECTIONS,
            ("POST", "/documento/secao/alterar"): _envelope("6"),
        }
    )
    result = await helpers.atualizar_documento_conteudo(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        documento="42",
        conteudo_html="<p>new</p>",
    )
    assert result["secoesAtualizadas"] == 1  # only principal changed
    assert result["novaVersao"] == "6"
    posted = client.calls[-1]["data"]
    assert posted["documento"] == "42"
    assert posted["versao"] == "5"
    payload = json.loads(posted["secoes"])
    assert len(payload) == 3  # SEI requires ALL sections
    assert payload[0]["id"] == "10"
    assert payload[0]["conteudo"] == "<p>new</p>"
    # untouched sections: html.unescape("&amp;lt;") → "&lt;"
    assert payload[1]["conteudo"] == "&lt;p&gt;old extra&lt;/p&gt;"
    assert payload[2]["conteudo"] == "&lt;p&gt;read only&lt;/p&gt;"


@pytest.mark.asyncio
async def test_atualizar_batch_mode_multiple_sections():
    client = _StubClient(
        {
            ("GET", "/documento/secao/listar"): _CURRENT_SECTIONS,
            ("POST", "/documento/secao/alterar"): _envelope("6"),
        }
    )
    result = await helpers.atualizar_documento_conteudo(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        documento="42",
        secoes=[
            {"id": "10", "conteudo": "<p>A</p>"},
            {"idSecaoModelo": "101", "conteudo": "<p>B</p>"},
        ],
    )
    assert result["secoesAtualizadas"] == 2
    payload = json.loads(client.calls[-1]["data"]["secoes"])
    assert len(payload) == 3  # SEI requires ALL sections
    assert {p["id"] for p in payload} == {"10", "11", "12"}


@pytest.mark.asyncio
async def test_atualizar_rejects_read_only_section():
    client = _StubClient(
        {("GET", "/documento/secao/listar"): _CURRENT_SECTIONS}
    )
    with pytest.raises(HelperError, match="read-only"):
        await helpers.atualizar_documento_conteudo(
            client=client,  # type: ignore[arg-type]
            unidade_override=None,
            documento="42",
            secoes=[{"id": "12", "conteudo": "<p>nope</p>"}],
        )


@pytest.mark.asyncio
async def test_atualizar_rejects_unknown_section():
    client = _StubClient(
        {("GET", "/documento/secao/listar"): _CURRENT_SECTIONS}
    )
    with pytest.raises(HelperError, match="does not match"):
        await helpers.atualizar_documento_conteudo(
            client=client,  # type: ignore[arg-type]
            unidade_override=None,
            documento="42",
            secoes=[{"id": "999", "conteudo": "<p>?</p>"}],
        )


@pytest.mark.asyncio
async def test_atualizar_rejects_mutual_exclusion():
    client = _StubClient({})
    with pytest.raises(HelperError, match="not both"):
        await helpers.atualizar_documento_conteudo(
            client=client,  # type: ignore[arg-type]
            unidade_override=None,
            documento="42",
            conteudo_html="<p>x</p>",
            secoes=[{"id": "10", "conteudo": "<p>y</p>"}],
        )


@pytest.mark.asyncio
async def test_atualizar_requires_one_mode():
    client = _StubClient({})
    with pytest.raises(HelperError, match="quick mode|batch mode"):
        await helpers.atualizar_documento_conteudo(
            client=client,  # type: ignore[arg-type]
            unidade_override=None,
            documento="42",
        )


# ── listar_processos_facil (read helper) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_listar_processos_facil_basic():
    """Default listing returns processes + pagination hints."""
    client = _StubClient(
        {
            ("GET", "/processo/listar"): {
                "sucesso": True,
                "mensagem": "",
                "data": [
                    {"id": "100", "protocolo": "TEST.000001/2025-01"},
                    {"id": "101", "protocolo": "TEST.000002/2025-02"},
                ],
                "total": 2,
            },
        }
    )
    result = await helpers.listar_processos_facil(
        client=client,  # type: ignore[arg-type]
        unidade_override="42",
        limit=10,
        start=0,
    )
    assert result["limit"] == 10
    assert result["start"] == 0
    assert len(result["processos"]) == 2
    assert result["processos"][0]["protocolo"] == "TEST.000001/2025-01"
    assert result["filtros_aplicados"]["apenasMeus"] == "S"
    assert result["paginacao"]["proxima"] is None  # 2 < 10, no next page
    assert result["paginacao"]["anterior"] is None
    assert not result["paginacao"]["tem_mais"]
    assert any("Use 'start' to paginate" in h for h in result["hints"])


@pytest.mark.asyncio
async def test_listar_processos_facil_pagination_has_more():
    """When results == limit, hints include next page offset."""
    client = _StubClient(
        {
            ("GET", "/processo/listar"): {
                "sucesso": True,
                "mensagem": "",
                "data": [{"id": str(i), "protocolo": f"P-{i}"} for i in range(10)],
                "total": 42,
            },
        }
    )
    result = await helpers.listar_processos_facil(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        limit=10,
        start=0,
    )
    assert len(result["processos"]) == 10
    assert result["paginacao"]["tem_mais"] is True
    assert result["paginacao"]["proxima"] == 10
    assert result["paginacao"]["anterior"] is None
    assert any("next page" in h.lower() for h in result["hints"])


@pytest.mark.asyncio
async def test_listar_processos_facil_mid_page():
    """When start > 0, both prev and next navigation are present."""
    client = _StubClient(
        {
            ("GET", "/processo/listar"): {
                "sucesso": True,
                "mensagem": "",
                "data": [{"id": str(i), "protocolo": f"P-{i}"} for i in range(10)],
                "total": 30,
            },
        }
    )
    result = await helpers.listar_processos_facil(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        limit=10,
        start=10,
    )
    assert result["start"] == 10
    assert result["paginacao"]["proxima"] == 20
    assert result["paginacao"]["anterior"] == 0


@pytest.mark.asyncio
async def test_listar_processos_facil_empty():
    """Empty results include hints about changing filters."""
    client = _StubClient(
        {
            ("GET", "/processo/listar"): {
                "sucesso": True,
                "mensagem": "",
                "data": [],
                "total": 0,
            },
        }
    )
    result = await helpers.listar_processos_facil(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        limit=10,
        start=0,
    )
    assert result["processos"] == []
    assert result["total"] == 0
    assert any("No processes found" in h for h in result["hints"])


@pytest.mark.asyncio
async def test_listar_processos_facil_with_filters():
    """Custom filters are reflected in filtros_aplicados."""
    client = _StubClient(
        {
            ("GET", "/processo/listar"): {
                "sucesso": True,
                "mensagem": "",
                "data": [],
                "total": 0,
            },
        }
    )
    result = await helpers.listar_processos_facil(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        limit=5,
        start=0,
        apenas_meus=False,
        tipo="G",
        usuario="j.silva",
        id_unidade="110000965",
    )
    assert result["filtros_aplicados"]["apenasMeus"] == "N"
    assert result["filtros_aplicados"]["tipo"] == "G"
    assert result["filtros_aplicados"]["usuario"] == "j.silva"
    assert result["filtros_aplicados"]["idUnidade"] == "110000965"


# ── ver_processo_completo (read helper) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_ver_processo_completo_full_run():
    """Metadata + documents + per-doc metadata in one call."""
    client = _StubClient(
        {
            ("GET", "/processo/123"): _envelope(
                {"idProcedimento": "123", "protocoloFormatado": "TEST.000001/2025-01"}
            ),
            ("GET", "/documento/listar/123"): _envelope(
                [
                    {"idDocumento": "5", "numero": "001"},
                    {"idDocumento": "7", "numero": "002"},
                ]
            ),
            ("GET", "/documento/interno/consultar/5"): _envelope(
                {"idDocumento": "5", "numero": "001", "tipo": "Ofício"}
            ),
            ("GET", "/documento/interno/consultar/7"): _envelope(
                {"idDocumento": "7", "numero": "002", "tipo": "Despacho"}
            ),
        }
    )
    result = await helpers.ver_processo_completo(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        protocolo="123",
    )
    assert result["protocolo"] == "123"
    assert result["metadados"]["protocoloFormatado"] == "TEST.000001/2025-01"
    assert result["total_documentos"] == 2
    assert len(result["documentos"]) == 2
    # Each document enriched with _metadados
    assert result["documentos"][0]["_metadados"]["tipo"] == "Ofício"
    assert result["documentos"][1]["_metadados"]["tipo"] == "Despacho"
    # 1 (meta) + 1 (docs) + 2 (per-doc meta) = 4 calls
    assert len(client.calls) == 4


@pytest.mark.asyncio
async def test_ver_processo_completo_skip_documents():
    """When incluir_documentos=False, only metadata is fetched."""
    client = _StubClient(
        {
            ("GET", "/processo/123"): _envelope({"idProcedimento": "123"}),
        }
    )
    result = await helpers.ver_processo_completo(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        protocolo="123",
        incluir_documentos=False,
    )
    assert result["protocolo"] == "123"
    assert result["metadados"]["idProcedimento"] == "123"
    assert "documentos" not in result
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_ver_processo_completo_skip_doc_metadata():
    """When incluir_metadados_documentos=False, docs listed without enrichment."""
    client = _StubClient(
        {
            ("GET", "/processo/123"): _envelope({"idProcedimento": "123"}),
            ("GET", "/documento/listar/123"): _envelope(
                [{"idDocumento": "5", "numero": "001"}]
            ),
        }
    )
    result = await helpers.ver_processo_completo(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        protocolo="123",
        incluir_metadados_documentos=False,
    )
    assert result["total_documentos"] == 1
    assert "_metadados" not in result["documentos"][0]
    assert len(client.calls) == 2  # meta + docs, no per-doc calls


@pytest.mark.asyncio
async def test_ver_processo_completo_doc_listing_failure():
    """When doc listing fails, the error is surfaced, not thrown."""
    client = _StubClient(
        {
            ("GET", "/processo/123"): _envelope({"idProcedimento": "123"}),
            ("GET", "/documento/listar/123"): SeiPenError(
                "endpoint unavailable", status_code=503
            ),
        }
    )
    result = await helpers.ver_processo_completo(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        protocolo="123",
    )
    assert result["metadados"]["idProcedimento"] == "123"
    assert result["documentos"] == []
    assert "endpoint unavailable" in result["documentos_error"]


@pytest.mark.asyncio
async def test_ver_processo_completo_per_doc_meta_failure():
    """When one doc's metadata fails, others are still enriched."""
    client = _StubClient(
        {
            ("GET", "/processo/123"): _envelope({"idProcedimento": "123"}),
            ("GET", "/documento/listar/123"): _envelope(
                [
                    {"idDocumento": "5", "numero": "001"},
                    {"idDocumento": "7", "numero": "002"},
                ]
            ),
            ("GET", "/documento/interno/consultar/5"): _envelope({"tipo": "OK"}),
            ("GET", "/documento/interno/consultar/7"): SeiPenError(
                "not found", status_code=404
            ),
        }
    )
    result = await helpers.ver_processo_completo(
        client=client,  # type: ignore[arg-type]
        unidade_override=None,
        protocolo="123",
    )
    assert result["documentos"][0]["_metadados"]["tipo"] == "OK"
    assert "_metadados_error" in result["documentos"][1]
    assert "not found" in result["documentos"][1]["_metadados_error"]


@pytest.mark.asyncio
async def test_ver_processo_completo_requires_protocolo():
    client = _StubClient({})
    with pytest.raises(HelperError, match="'protocolo'"):
        await helpers.ver_processo_completo(
            client=client,  # type: ignore[arg-type]
            unidade_override=None,
            protocolo="",
        )


# ── criar_processo_facil auto-fetch (write helper) ──────────────────────────


@pytest.mark.asyncio
async def test_criar_processo_facil_auto_fetch_types():
    """When no tipo_processo_id or tipo_processo_nome, candidates are returned."""
    client = _StubClient(
        {
            ("GET", "/processo/tipo/listar"): _envelope(
                [
                    {"idTipoProcesso": "1", "nomeTipo": "Administrativo"},
                    {"idTipoProcesso": "2", "nomeTipo": "Disciplinar"},
                ]
            ),
        }
    )
    with pytest.raises(HelperError) as exc_info:
        await helpers.criar_processo_facil(
            client=client,  # type: ignore[arg-type]
            base_url="http://test",
            orgao_id="0",
            org_id=None,
            session=None,
            unidade_override=None,
            tipo_processo_nome=None,
            tipo_processo_id=None,
            nivel_acesso=0,
            hipotese_nome=None,
            hipotese_id=None,
        )
    assert "Process type is required" in str(exc_info.value)
    assert len(exc_info.value.candidates) == 2
    assert exc_info.value.candidates[0]["nome"] == "Administrativo"
    assert exc_info.value.candidates[1]["nome"] == "Disciplinar"


@pytest.mark.asyncio
async def test_criar_processo_facil_auto_fetch_hipotese():
    """When nivelAcesso > 0 and no hipotese, legal hypotheses are returned."""
    client = _StubClient(
        {
            ("GET", "/hipoteseLegal/pesquisar"): _envelope(
                [
                    {"idHipoteseLegal": "10", "nome": "Sigilo Fiscal"},
                    {"idHipoteseLegal": "20", "nome": "Segurança Nacional"},
                ]
            ),
        }
    )
    with pytest.raises(HelperError) as exc_info:
        await helpers.criar_processo_facil(
            client=client,  # type: ignore[arg-type]
            base_url="http://test",
            orgao_id="0",
            org_id=None,
            session=None,
            unidade_override=None,
            tipo_processo_nome=None,
            tipo_processo_id="1",
            nivel_acesso=1,
            hipotese_nome=None,
            hipotese_id=None,
        )
    assert "legal hypothesis" in str(exc_info.value).lower()
    assert len(exc_info.value.candidates) == 2
    assert exc_info.value.candidates[0]["nome"] == "Sigilo Fiscal"


@pytest.mark.asyncio
async def test_criar_processo_facil_normal_flow():
    """When tipo_processo_id is provided, normal creation proceeds (auto-selects single subject)."""
    client = _StubClient(
        {
            ("GET", "/processo/assunto/sugestao/1/listar"): _envelope(
                [{"idAssunto": "100", "nome": "Material de Consumo"}]
            ),
            ("POST", "/processo/criar"): _envelope(
                {"IdProcedimento": "999", "ProtocoloFormatado": "TEST.000001/2025-99"}
            ),
        }
    )
    result = await helpers.criar_processo_facil(
        client=client,  # type: ignore[arg-type]
        base_url="http://test",
        orgao_id="0",
        org_id=None,
        session=None,
        unidade_override=None,
        tipo_processo_nome=None,
        tipo_processo_id="1",
        nivel_acesso=0,
        hipotese_nome=None,
        hipotese_id=None,
    )
    assert result["defaults_aplicados"]["nivelAcesso"] == "0 (público)"
    assert "auto-selected: 100" in result["defaults_aplicados"]["assuntos"]
    payload = result["result"]
    assert payload["IdProcedimento"] == "999"


@pytest.mark.asyncio
async def test_criar_processo_facil_auto_select_single_subject():
    """When exactly one subject is available, auto-select it without error."""
    client = _StubClient(
        {
            ("GET", "/processo/assunto/sugestao/47/listar"): _envelope(
                [{"idAssunto": "200", "nome": "Administrativo"}]
            ),
            ("POST", "/processo/criar"): _envelope(
                {"IdProcedimento": "777", "ProtocoloFormatado": "P-777"}
            ),
        }
    )
    result = await helpers.criar_processo_facil(
        client=client,  # type: ignore[arg-type]
        base_url="http://test",
        orgao_id="0",
        org_id=None,
        session=None,
        unidade_override=None,
        tipo_processo_nome=None,
        tipo_processo_id="47",
        nivel_acesso=0,
        hipotese_nome=None,
        hipotese_id=None,
    )
    assert result["defaults_aplicados"]["assuntos"] == "auto-selected: 200"
    # Verify the POST form included the auto-selected subject
    posted = client.calls[-1]["data"]
    assert posted["assuntos"] == "200"


@pytest.mark.asyncio
async def test_criar_processo_facil_multi_subject_returns_candidates():
    """When multiple subjects exist, return them as candidates."""
    client = _StubClient(
        {
            ("GET", "/processo/assunto/sugestao/47/listar"): _envelope(
                [
                    {"idAssunto": "033.42", "nome": "MATERIAL DE CONSUMO"},
                    {"idAssunto": "010.10", "nome": "SOLICITAÇÃO DE COMPRAS"},
                ]
            ),
        }
    )
    with pytest.raises(HelperError) as exc_info:
        await helpers.criar_processo_facil(
            client=client,  # type: ignore[arg-type]
            base_url="http://test",
            orgao_id="0",
            org_id=None,
            session=None,
            unidade_override=None,
            tipo_processo_nome=None,
            tipo_processo_id="47",
            nivel_acesso=0,
            hipotese_nome=None,
            hipotese_id=None,
        )
    assert "assuntos" in str(exc_info.value).lower()
    assert len(exc_info.value.candidates) == 2
    assert exc_info.value.candidates[0]["nome"] == "MATERIAL DE CONSUMO"
    assert "assuntos='033.42'" in str(exc_info.value)


@pytest.mark.asyncio
async def test_criar_processo_facil_no_subjects_error():
    """When no subjects exist for the process type, give a clear error."""
    client = _StubClient(
        {
            ("GET", "/processo/assunto/sugestao/99/listar"): _envelope([]),
        }
    )
    with pytest.raises(HelperError) as exc_info:
        await helpers.criar_processo_facil(
            client=client,  # type: ignore[arg-type]
            base_url="http://test",
            orgao_id="0",
            org_id=None,
            session=None,
            unidade_override=None,
            tipo_processo_nome=None,
            tipo_processo_id="99",
            nivel_acesso=0,
            hipotese_nome=None,
            hipotese_id=None,
        )
    assert "no subjects were found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_criar_processo_facil_explicit_assuntos():
    """When assuntos is explicitly provided, validate compatibility."""
    # Subject "42" IS in the suggested list → OK
    client = _StubClient(
        {
            ("GET", "/processo/assunto/sugestao/1/listar"): _envelope(
                [
                    {"idAssunto": "42", "nome": "Material de Consumo"},
                    {"idAssunto": "99", "nome": "Outro"},
                ]
            ),
            ("POST", "/processo/criar"): _envelope(
                {"IdProcedimento": "555", "ProtocoloFormatado": "P-555"}
            ),
        }
    )
    result = await helpers.criar_processo_facil(
        client=client,  # type: ignore[arg-type]
        base_url="http://test",
        orgao_id="0",
        org_id=None,
        session=None,
        unidade_override=None,
        tipo_processo_nome=None,
        tipo_processo_id="1",
        nivel_acesso=0,
        hipotese_nome=None,
        hipotese_id=None,
        assuntos="42",
    )
    assert result["result"]["IdProcedimento"] == "555"
    assert client.calls[-1]["data"]["assuntos"] == "42"


@pytest.mark.asyncio
async def test_criar_processo_facil_incompatible_subject():
    """When the provided subject is not in the suggested list, return error with candidates."""
    client = _StubClient(
        {
            ("GET", "/processo/assunto/sugestao/1/listar"): _envelope(
                [
                    {"idAssunto": "10", "nome": "Administrativo"},
                    {"idAssunto": "20", "nome": "Financeiro"},
                ]
            ),
        }
    )
    with pytest.raises(HelperError) as exc_info:
        await helpers.criar_processo_facil(
            client=client,  # type: ignore[arg-type]
            base_url="http://test",
            orgao_id="0",
            org_id=None,
            session=None,
            unidade_override=None,
            tipo_processo_nome=None,
            tipo_processo_id="1",
            nivel_acesso=0,
            hipotese_nome=None,
            hipotese_id=None,
            assuntos="252",  # NOT in the suggested list
        )
    assert "not compatible" in str(exc_info.value).lower()
    assert len(exc_info.value.candidates) == 2
    assert exc_info.value.candidates[0]["id"] == "10"


@pytest.mark.asyncio
async def test_criar_processo_facil_defaults_applied():
    """When optional params are omitted, defaults_aplicados reflects them."""
    client = _StubClient(
        {
            ("GET", "/processo/assunto/sugestao/1/listar"): _envelope(
                [{"idAssunto": "100", "nome": "Geral"}]
            ),
            ("POST", "/processo/criar"): _envelope(
                {"IdProcedimento": "888", "ProtocoloFormatado": "P-888"}
            ),
        }
    )
    result = await helpers.criar_processo_facil(
        client=client,  # type: ignore[arg-type]
        base_url="http://test",
        orgao_id="0",
        org_id=None,
        session=None,
        unidade_override=None,
        tipo_processo_nome=None,
        tipo_processo_id="1",
        nivel_acesso=0,
        hipotese_nome=None,
        hipotese_id=None,
        especificacao=None,
        observacoes=None,
    )
    defaults = result["defaults_aplicados"]
    assert defaults["nivelAcesso"] == "0 (público)"
    assert defaults["especificacao"] == "(vazio)"
    assert defaults["observacoes"] == "(vazio)"
    assert "auto-selected: 100" in defaults["assuntos"]


# ── criar_documento_com_conteudo auto-fetch + ensure_ascii ───────────────────


@pytest.mark.asyncio
async def test_criar_documento_com_conteudo_auto_fetch_series():
    """When no id_serie or serie_nome, document type candidates are returned."""
    client = _StubClient(
        {
            ("GET", "/documento/tipo/pesquisar"): _envelope(
                [
                    {"idSerie": "306", "nomeSerie": "Memorando"},
                    {"idSerie": "200", "nomeSerie": "Ofício"},
                ]
            ),
        }
    )
    with pytest.raises(HelperError) as exc_info:
        await helpers.criar_documento_com_conteudo(
            client=client,  # type: ignore[arg-type]
            base_url="http://test",
            orgao_id="0",
            org_id=None,
            session=None,
            unidade_override=None,
            procedimento="100",
            serie_nome=None,
            id_serie=None,
            nivel_acesso=0,
            observacao="",
            conteudo_html="<p>test</p>",
            id_unidade_geradora=None,
        )
    assert "Document type" in str(exc_info.value)
    assert len(exc_info.value.candidates) == 2
    assert exc_info.value.candidates[0]["nome"] == "Memorando"


@pytest.mark.asyncio
async def test_criar_documento_com_conteudo_ensure_ascii():
    """The secoes JSON sent to SEI uses ensure_ascii=True."""
    client = _StubClient(
        {
            ("POST", "/documento/100/interno/criar"): _envelope(
                {"idDocumento": "55", "protocoloDocumentoFormatado": "DOC-55"}
            ),
            ("GET", "/documento/secao/listar"): _envelope(
                {
                    "ultimaVersaoDocumento": "1",
                    "secoes": [
                        {
                            "id": "10",
                            "idSecaoModelo": "100",
                            "PrincipalSecaoDocumento": "S",
                            "somenteLeitura": "N",
                            "conteudo": "",
                        }
                    ],
                }
            ),
            ("POST", "/documento/secao/alterar"): _envelope("2"),
        }
    )
    result = await helpers.criar_documento_com_conteudo(
        client=client,  # type: ignore[arg-type]
        base_url="http://test",
        orgao_id="0",
        org_id=None,
        session=None,
        unidade_override=None,
        procedimento="100",
        serie_nome=None,
        id_serie="306",
        nivel_acesso=0,
        observacao="Test doc",
        conteudo_html="<p>Conteúdo com acentuação</p>",
        id_unidade_geradora="110000965",
    )
    assert result["idDocumento"] == "55"
    assert result["defaults_aplicados"]["nivelAcesso"] == "0 (público)"
    # Verify the posted secoes JSON uses ensure_ascii=True:
    # non-ASCII chars like 'ú' and 'ç' become \uXXXX escapes.
    posted = client.calls[-1]["data"]
    secoes_json = posted["secoes"]
    assert isinstance(secoes_json, str)
    # ensure_ascii=True means no raw non-ASCII bytes in the JSON string
    # 'ç' → \u00e7, 'ú' → \u00fa
    assert "\\u00e7" in secoes_json or "\\u00fa" in secoes_json
    # The raw chars should NOT appear
    assert "ç" not in secoes_json
    assert "ú" not in secoes_json


@pytest.mark.asyncio
async def test_criar_documento_com_conteudo_normal_flow():
    """Full 3-step creation with id_serie provided directly."""
    client = _StubClient(
        {
            ("POST", "/documento/100/interno/criar"): _envelope(
                {"idDocumento": "55", "protocoloDocumentoFormatado": "DOC-55"}
            ),
            ("GET", "/documento/secao/listar"): _envelope(
                {
                    "ultimaVersaoDocumento": "1",
                    "secoes": [
                        {
                            "id": "10",
                            "idSecaoModelo": "100",
                            "PrincipalSecaoDocumento": "S",
                            "somenteLeitura": "N",
                            "conteudo": "",
                        },
                        {
                            "id": "11",
                            "idSecaoModelo": "101",
                            "PrincipalSecaoDocumento": "N",
                            "somenteLeitura": "S",
                            "conteudo": "read-only placeholder",
                        },
                    ],
                }
            ),
            ("POST", "/documento/secao/alterar"): _envelope("2"),
        }
    )
    result = await helpers.criar_documento_com_conteudo(
        client=client,  # type: ignore[arg-type]
        base_url="http://test",
        orgao_id="0",
        org_id=None,
        session=None,
        unidade_override=None,
        procedimento="100",
        serie_nome=None,
        id_serie="306",
        nivel_acesso=0,
        observacao="Test doc",
        conteudo_html="<p>Hello</p>",
        id_unidade_geradora="110000965",
    )
    assert result["idDocumento"] == "55"
    assert result["protocoloDocumentoFormatado"] == "DOC-55"
    assert result["secoesAtualizadas"] == 1  # only the principal editable
    assert result["defaults_aplicados"]["nivelAcesso"] == "0 (público)"
    # 3 calls: criar + secao_listar + secao_alterar
    assert len(client.calls) == 3

