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
from custom_code.tools.sei_pen._client import SeiPenError, instructive_hint, with_hint
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
                "conteudo": "<p>old principal</p>",
            },
            {
                "id": "11",
                "idSecaoModelo": "101",
                "PrincipalSecaoDocumento": "N",
                "somenteLeitura": "N",
                "conteudo": "<p>old extra</p>",
            },
            {
                "id": "12",
                "idSecaoModelo": "102",
                "PrincipalSecaoDocumento": "N",
                "somenteLeitura": "S",
                "conteudo": "<p>read only</p>",
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
    assert result["secoesAtualizadas"] == 1
    assert result["novaVersao"] == "6"
    posted = client.calls[-1]["data"]
    assert posted["documento"] == "42"
    assert posted["versao"] == "5"
    payload = json.loads(posted["secoes"])
    assert len(payload) == 1
    assert payload[0]["id"] == "10"
    assert payload[0]["conteudo"] == "<p>new</p>"


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
    assert {p["id"] for p in payload} == {"10", "11"}


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

