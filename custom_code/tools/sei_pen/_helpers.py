"""SEI-PEN high-level helpers — ID resolution and multi-call chains.

These helpers let the agent work with friendly inputs (type/série *names*,
HTML content, deadline in days) instead of raw SEI numeric IDs. They resolve IDs
through the cached reference loaders in :mod:`._cache` and orchestrate the
multi-call sequences the SEI API requires (e.g. create-document-with-content is
a 3-call chain).

All resolvers return ``(resolved_id, candidates)``: when exactly one match is
found ``resolved_id`` is set; when zero or many match it is ``None`` and
``candidates`` lists the options so the tool can return an instructive error.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from custom_code.tools.sei_pen import _cache
from custom_code.tools.sei_pen._client import SeiPenClient, SeiPenError

log = logging.getLogger(__name__)

# Access-level friendly labels (SEI nivelAcesso enum).
NIVEL_ACESSO_LABELS: dict[int, str] = {
    0: "público",
    1: "restrito",
    2: "sigiloso",
}

# Candidate keys used to read an id/name from heterogeneous SEI list records.
_ID_KEYS = (
    "idSerie", "idTipoProcedimento", "idTipoProcesso", "idHipoteseLegal",
    "idUnidade", "idContato", "id",
)
_NAME_KEYS = (
    "nome", "nomeSerie", "nomeTipo", "descricao", "sigla", "nomeUnidade",
)


def _record_id(record: dict) -> Optional[str]:
    for key in _ID_KEYS:
        value = record.get(key)
        if value not in (None, "", []):
            return str(value)
    return None


def _record_name(record: dict) -> Optional[str]:
    for key in _NAME_KEYS:
        value = record.get(key)
        if value not in (None, "", []):
            return str(value)
    return None


def _norm(text: str) -> str:
    return " ".join(str(text).strip().lower().split())


def match_by_name(
    records: list[dict], name: str
) -> tuple[Optional[str], list[dict]]:
    """Resolve a record id by name.

    Exact (case-insensitive) match wins; otherwise substring matches are
    returned as candidates. Returns ``(id, candidates)``.
    """
    target = _norm(name)
    exact: list[dict] = []
    partial: list[dict] = []
    for rec in records:
        rec_name = _record_name(rec)
        if not rec_name:
            continue
        norm = _norm(rec_name)
        if norm == target:
            exact.append(rec)
        elif target in norm:
            partial.append(rec)

    if len(exact) == 1:
        return _record_id(exact[0]), []
    if not exact and len(partial) == 1:
        return _record_id(partial[0]), []

    candidates = exact or partial
    summary = [
        {"id": _record_id(r), "nome": _record_name(r)}
        for r in candidates[:25]
    ]
    return None, summary


# ── Resolvers (cached) ────────────────────────────────────────────────────────


async def resolve_serie(
    *,
    client: SeiPenClient,
    base_url: str,
    orgao_id: str,
    unidade: Optional[str],
    name: str,
    org_id: Optional[uuid.UUID],
    session: Optional[AsyncSession],
) -> tuple[Optional[str], list[dict]]:
    """Resolve a document type (série) ID by name."""
    records = await _cache.load_series(
        client=client,
        base_url=base_url,
        orgao_id=orgao_id,
        unidade=unidade,
        org_id=org_id,
        session=session,
    )
    return match_by_name(records, name)


async def resolve_tipo_processo(
    *,
    client: SeiPenClient,
    base_url: str,
    orgao_id: str,
    name: str,
    org_id: Optional[uuid.UUID],
    session: Optional[AsyncSession],
) -> tuple[Optional[str], list[dict]]:
    """Resolve a process type ID by name."""
    records = await _cache.load_tipos_processo(
        client=client,
        base_url=base_url,
        orgao_id=orgao_id,
        org_id=org_id,
        session=session,
    )
    return match_by_name(records, name)


async def resolve_hipotese(
    *,
    client: SeiPenClient,
    base_url: str,
    orgao_id: str,
    nivel_acesso: int,
    name: Optional[str],
    org_id: Optional[uuid.UUID],
    session: Optional[AsyncSession],
) -> tuple[Optional[str], list[dict]]:
    """Resolve a legal-hypothesis ID for an access level (by name when given).

    When *name* is omitted and exactly one hypothesis exists for the level it is
    auto-selected; otherwise all options are returned as candidates.
    """
    records = await _cache.load_hipoteses(
        client=client,
        base_url=base_url,
        orgao_id=orgao_id,
        nivel_acesso=nivel_acesso,
        org_id=org_id,
        session=session,
    )
    if name:
        return match_by_name(records, name)
    if len(records) == 1:
        return _record_id(records[0]), []
    return None, [
        {"id": _record_id(r), "nome": _record_name(r)} for r in records[:25]
    ]


# ── Multi-call chains ─────────────────────────────────────────────────────────


class HelperError(Exception):
    """Raised by chain helpers with an instructive message and optional candidates."""

    def __init__(self, message: str, candidates: Optional[list[dict]] = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


def _extract_data(body: dict) -> Any:
    return body.get("data")


async def criar_processo_facil(
    *,
    client: SeiPenClient,
    base_url: str,
    orgao_id: str,
    org_id: Optional[uuid.UUID],
    session: Optional[AsyncSession],
    unidade_override: Optional[str],
    tipo_processo_nome: Optional[str],
    tipo_processo_id: Optional[str],
    nivel_acesso: int,
    hipotese_nome: Optional[str],
    hipotese_id: Optional[str],
    grau_sigilo: str = "",
    especificacao: Optional[str] = None,
    interessados: Optional[str] = None,
    assuntos: Optional[str] = None,
    observacoes: Optional[str] = None,
) -> dict:
    """Create a process resolving type/hypothesis by name when needed."""
    # Resolve process type.
    resolved_tipo = tipo_processo_id
    if not resolved_tipo:
        if not tipo_processo_nome:
            raise HelperError(
                "Provide 'tipo_processo_id' or 'tipo_processo_nome'. List types with "
                "sei_pen_read(operation='processo.tipo_listar')."
            )
        resolved_tipo, candidates = await resolve_tipo_processo(
            client=client, base_url=base_url, orgao_id=orgao_id,
            name=tipo_processo_nome, org_id=org_id, session=session,
        )
        if not resolved_tipo:
            raise HelperError(
                f"Could not uniquely resolve process type '{tipo_processo_nome}'. "
                "Pass 'tipo_processo_id' from the candidates.",
                candidates,
            )

    # Resolve legal hypothesis when access is restricted/secret.
    resolved_hip = hipotese_id
    if nivel_acesso and nivel_acesso > 0 and not resolved_hip:
        resolved_hip, candidates = await resolve_hipotese(
            client=client, base_url=base_url, orgao_id=orgao_id,
            nivel_acesso=nivel_acesso, name=hipotese_nome,
            org_id=org_id, session=session,
        )
        if not resolved_hip:
            raise HelperError(
                f"Access level {nivel_acesso} requires a legal hypothesis; could not "
                "resolve one automatically. Pass 'hipotese_id' from the candidates.",
                candidates,
            )

    form: dict[str, Any] = {
        "tipoProcesso": resolved_tipo,
        "nivelAcesso": nivel_acesso,
        "hipoteseLegal": resolved_hip or "",
        "grauSigilo": grau_sigilo or "",
    }
    for key, value in (
        ("especificacao", especificacao),
        ("interessados", interessados),
        ("assuntos", assuntos),
        ("observacoes", observacoes),
    ):
        if value not in (None, ""):
            form[key] = value

    body = await client.request(
        "POST", "/processo/criar", data=form, unidade_override=unidade_override
    )
    return {
        "resolved": {"tipoProcesso": resolved_tipo, "hipoteseLegal": resolved_hip},
        "result": _extract_data(body),
    }


async def criar_documento_com_conteudo(
    *,
    client: SeiPenClient,
    base_url: str,
    orgao_id: str,
    org_id: Optional[uuid.UUID],
    session: Optional[AsyncSession],
    unidade_override: Optional[str],
    procedimento: str,
    serie_nome: Optional[str],
    id_serie: Optional[str],
    nivel_acesso: int,
    observacao: str,
    conteudo_html: str,
    id_unidade_geradora: Optional[str],
    id_hipotese_legal: Optional[str] = None,
) -> dict:
    """Create an internal document and write its body in one shot (3-call chain).

    1. ``POST /documento/{procedimento}/interno/criar`` → ``idDocumento``
    2. ``GET  /documento/secao/listar?id=<idDocumento>`` → sections + last version
    3. ``POST /documento/secao/alterar`` → writes ``conteudo_html`` into the
       editable section(s)
    """
    # Resolve série.
    resolved_serie = id_serie
    if not resolved_serie:
        if not serie_nome:
            raise HelperError(
                "Provide 'id_serie' or 'serie_nome'. List document types with "
                "sei_pen_read(operation='documento.tipo_pesquisar')."
            )
        resolved_serie, candidates = await resolve_serie(
            client=client, base_url=base_url, orgao_id=orgao_id,
            unidade=unidade_override, name=serie_nome,
            org_id=org_id, session=session,
        )
        if not resolved_serie:
            raise HelperError(
                f"Could not uniquely resolve document type '{serie_nome}'. "
                "Pass 'id_serie' from the candidates.",
                candidates,
            )

    # 1) Create the internal document.
    create_form: dict[str, Any] = {
        "idSerie": resolved_serie,
        "observacao": observacao,
        "nivelAcesso": nivel_acesso,
    }
    if id_unidade_geradora:
        create_form["idUnidadeGeradoraProtocolo"] = id_unidade_geradora
    if id_hipotese_legal:
        create_form["idHipoteseLegal"] = id_hipotese_legal

    create_body = await client.request(
        "POST",
        f"/documento/{procedimento}/interno/criar",
        data=create_form,
        unidade_override=unidade_override,
    )
    created = _extract_data(create_body) or {}
    id_documento = str(created.get("idDocumento") or "")
    if not id_documento:
        raise HelperError(
            "Document was created but the API returned no idDocumento; cannot write "
            "content. Inspect the create response."
        )

    # 2) List sections + last version.
    secao_body = await client.request(
        "GET",
        "/documento/secao/listar",
        params={"id": id_documento},
        unidade_override=unidade_override,
    )
    secao_data = _extract_data(secao_body) or {}
    secoes = secao_data.get("secoes") or []
    versao = secao_data.get("ultimaVersaoDocumento")

    # 3) Write content into editable (non read-only) sections. When the model
    # has a single editable section, fill it; otherwise fill the principal one.
    editable = [
        s for s in secoes
        if str(s.get("somenteLeitura", "N")).upper() != "S"
    ]
    if not editable:
        editable = secoes
    principal = [s for s in editable if str(s.get("PrincipalSecaoDocumento", "")).upper() == "S"]
    targets = principal or editable[:1] or editable

    payload = [
        {
            "id": s.get("id"),
            "idSecaoModelo": s.get("idSecaoModelo"),
            "conteudo": conteudo_html,
        }
        for s in targets
    ]

    alterar_form = {
        "documento": id_documento,
        "versao": versao,
        "secoes": json.dumps(payload, ensure_ascii=False),
    }
    alterar_body = await client.request(
        "POST",
        "/documento/secao/alterar",
        data=alterar_form,
        unidade_override=unidade_override,
    )

    return {
        "idDocumento": id_documento,
        "protocoloDocumentoFormatado": created.get("protocoloDocumentoFormatado"),
        "idSerie": resolved_serie,
        "versaoAnterior": versao,
        "novaVersao": _extract_data(alterar_body),
        "secoesAtualizadas": len(payload),
    }


def _compute_dt_programada(
    *, dias: Optional[int], data: Optional[str]
) -> str:
    """Return a dd/MM/yyyy deadline date from an explicit date or N days ahead."""
    if data:
        return data
    if dias is None:
        raise HelperError("Provide 'dias' (days ahead) or 'data' (dd/MM/yyyy).")
    target = datetime.now() + timedelta(days=int(dias))
    return target.strftime("%d/%m/%Y")


async def definir_prazo(
    *,
    client: SeiPenClient,
    unidade_override: Optional[str],
    unidade: Optional[str],
    dias: Optional[int],
    data: Optional[str],
    usuario: Optional[str] = None,
    atividade_envio: Optional[str] = None,
) -> dict:
    """Schedule a programmed return (deadline) for the process in a unit."""
    target_unit = unidade or unidade_override
    if not target_unit:
        raise HelperError(
            "Provide 'unidade' (target unit) or rely on the session unit override."
        )
    dt_programada = _compute_dt_programada(dias=dias, data=data)
    form: dict[str, Any] = {"unidade": target_unit, "dtProgramada": dt_programada}
    if usuario:
        form["usuario"] = usuario
    if atividade_envio:
        form["atividadeEnvio"] = atividade_envio

    body = await client.request(
        "POST",
        "/processo/agendar/retorno/programado",
        data=form,
        unidade_override=unidade_override,
    )
    return {"dtProgramada": dt_programada, "result": _extract_data(body)}


# ── Document content helpers ─────────────────────────────────────────────────


def _is_editable(secao: dict) -> bool:
    return str(secao.get("somenteLeitura", "N")).upper() != "S"


def _is_principal(secao: dict) -> bool:
    return str(secao.get("PrincipalSecaoDocumento", "")).upper() == "S"


async def ver_documento_completo(
    *,
    client: SeiPenClient,
    unidade_override: Optional[str],
    documento: str,
    incluir_visualizacao: bool = True,
    incluir_secoes: bool = True,
) -> dict:
    """Consolidated document view — metadata, rendered HTML and structured sections.

    Runs up to three reads in parallel to collapse the agent's typical N+1
    pattern (list documents → fetch each doc's content) into a single tool
    call. The rendered HTML (``documento.visualizar``) and the editable
    sections (``documento.secao_listar``) serve different purposes and are
    therefore both returned, instead of replacing one with the other.
    """
    if not documento:
        raise HelperError("'documento' (internal document ID) is required.")

    async def _meta() -> Any:
        body = await client.request(
            "GET",
            f"/documento/interno/consultar/{documento}",
            unidade_override=unidade_override,
        )
        return _extract_data(body)

    async def _html() -> Any:
        body = await client.request(
            "GET",
            f"/documento/{documento}/interno/visualizar",
            unidade_override=unidade_override,
        )
        return _extract_data(body)

    async def _secoes() -> Any:
        body = await client.request(
            "GET",
            "/documento/secao/listar",
            params={"id": documento},
            unidade_override=unidade_override,
        )
        return _extract_data(body)

    # Each call is wrapped so a failure in one branch (e.g. server returns
    # sucesso:false for visualizar on a non-text doc) does not lose the others.
    async def _safe(coro_fn):
        try:
            return await coro_fn()
        except SeiPenError as exc:
            return {"_error": str(exc), "_status_code": exc.status_code}

    tasks: list = [_safe(_meta)]
    tasks.append(_safe(_html) if incluir_visualizacao else _noop())
    tasks.append(_safe(_secoes) if incluir_secoes else _noop())
    meta_res, html_res, secoes_res = await asyncio.gather(*tasks)

    result: dict[str, Any] = {
        "documento": documento,
        "metadados": meta_res,
    }

    if incluir_visualizacao:
        result["html_renderizado"] = html_res

    if incluir_secoes:
        if isinstance(secoes_res, dict) and "_error" in secoes_res:
            result["secoes"] = []
            result["versao"] = None
            result["secoes_error"] = secoes_res["_error"]
        else:
            data = secoes_res or {}
            secoes_raw = (
                data.get("secoes") if isinstance(data, dict) else None
            ) or []
            result["secoes"] = [
                {
                    "id": s.get("id"),
                    "idSecaoModelo": s.get("idSecaoModelo"),
                    "principal": _is_principal(s),
                    "somenteLeitura": str(s.get("somenteLeitura", "N")).upper() == "S",
                    "dinamica": str(s.get("DinamicaSecaoDocumento", "")).upper() == "S",
                    "conteudo": s.get("conteudo"),
                }
                for s in secoes_raw
            ]
            result["versao"] = (
                data.get("ultimaVersaoDocumento") if isinstance(data, dict) else None
            )

    return result


async def _noop() -> None:
    return None


def _resolve_new_conteudo(
    secoes: list[dict],
    section: dict,
    by_id: dict[str, dict],
    by_modelo: dict[str, dict],
) -> str:
    """Find the new ``conteudo`` for *section* in the user-supplied *secoes* list.

    Matching is by ``id`` first, then ``idSecaoModelo``.
    """
    sid = str(section.get("id", ""))
    smodelo = str(section.get("idSecaoModelo", ""))
    for item in secoes:
        item_id = str(item.get("id", "")) if item.get("id") is not None else ""
        item_modelo = (
            str(item.get("idSecaoModelo", ""))
            if item.get("idSecaoModelo") is not None
            else ""
        )
        if item_id and item_id == sid:
            return str(item["conteudo"])
        if item_modelo and item_modelo == smodelo:
            return str(item["conteudo"])
    # Should not happen — caller guarantees a match.
    return str(section.get("conteudo") or "")


async def atualizar_documento_conteudo(
    *,
    client: SeiPenClient,
    unidade_override: Optional[str],
    documento: str,
    conteudo_html: Optional[str] = None,
    secoes: Optional[list[dict]] = None,
) -> dict:
    """Update document section contents in a single batched call.

    Two friendly modes (mutually exclusive):

    *Quick mode* — pass ``conteudo_html`` to overwrite the editable principal
    section(s), mirroring :func:`criar_documento_com_conteudo`.

    *Batch mode* — pass ``secoes`` as a list of
    ``{id|idSecaoModelo, conteudo}`` items. The helper looks up the current
    document version, matches each input against the current sections (by
    ``id`` first, then ``idSecaoModelo``), refuses read-only sections, and
    posts every change in one ``POST /documento/secao/alterar`` request.
    """
    if not documento:
        raise HelperError("'documento' (internal document ID) is required.")
    if conteudo_html is None and not secoes:
        raise HelperError(
            "Provide 'conteudo_html' (quick mode) or 'secoes' (batch mode)."
        )
    if conteudo_html is not None and secoes:
        raise HelperError(
            "Pass either 'conteudo_html' or 'secoes', not both."
        )

    # 1) Read current sections + last version.
    secao_body = await client.request(
        "GET",
        "/documento/secao/listar",
        params={"id": documento},
        unidade_override=unidade_override,
    )
    current_data = _extract_data(secao_body) or {}
    current = current_data.get("secoes") or []
    versao = current_data.get("ultimaVersaoDocumento")
    if not current:
        raise HelperError(
            f"Document {documento} has no listable sections; cannot update content."
        )

    by_id = {str(s.get("id")): s for s in current if s.get("id") is not None}
    by_modelo = {
        str(s.get("idSecaoModelo")): s
        for s in current
        if s.get("idSecaoModelo") is not None
    }

    # 2) Build a full payload — SEI EditorRN::adicionarVersaoInternoControlado
    #    REQUIRES exactly the same number of sections as in the database.
    #    Sending a subset throws "Conteúdo do documento incompleto."
    #    We must include every section, keeping the original content for
    #    sections the caller did not touch.
    changed: dict[str, None] = {}  # keys: str(id) or str(idSecaoModelo)

    if conteudo_html is not None:
        editable = [s for s in current if _is_editable(s)]
        principal = [s for s in editable if _is_principal(s)]
        targets = principal or editable[:1] or editable
        if not targets:
            raise HelperError(
                f"Document {documento} has no editable section to write into."
            )
        for s in targets:
            changed[str(s.get("id"))] = None
    else:
        assert secoes is not None
        for idx, item in enumerate(secoes):
            if not isinstance(item, dict):
                raise HelperError(
                    f"secoes[{idx}] must be an object with id/idSecaoModelo + conteudo."
                )
            if item.get("conteudo") is None:
                raise HelperError(f"secoes[{idx}].conteudo is required.")
            ref_id = item.get("id")
            ref_modelo = item.get("idSecaoModelo")
            match: Optional[dict] = None
            if ref_id is not None and str(ref_id) in by_id:
                match = by_id[str(ref_id)]
            elif ref_modelo is not None and str(ref_modelo) in by_modelo:
                match = by_modelo[str(ref_modelo)]
            if match is None:
                raise HelperError(
                    f"secoes[{idx}] does not match any current section of "
                    f"document {documento} (id={ref_id!r}, idSecaoModelo={ref_modelo!r})."
                )
            if not _is_editable(match):
                raise HelperError(
                    f"secoes[{idx}] targets a read-only section "
                    f"(id={match.get('id')}); cannot be updated."
                )
            changed[str(match.get("id"))] = None

    # Build full payload: every section, original or new content.
    # SEI's dataToUtf8 applies htmlspecialchars BEFORE utf8_encode on every
    # read, so section content comes back with double-encoded HTML entities
    # (e.g. &amp;lt; instead of &lt;).  Before sending untouched content
    # back we must unescape it, or the stored value gets corrupted
    # progressively at each roundtrip.
    payload: list[dict[str, Any]] = []
    for s in current:
        sid = str(s.get("id"))
        if sid in changed:
            new_conteudo = (
                str(conteudo_html)
                if conteudo_html is not None
                else _resolve_new_conteudo(secoes or [], s, by_id, by_modelo)
            )
        else:
            new_conteudo = _html.unescape(s.get("conteudo") or "")
        payload.append(
            {
                "id": s.get("id"),
                "idSecaoModelo": s.get("idSecaoModelo"),
                "conteudo": new_conteudo,
            }
        )

    if not payload:
        raise HelperError("No sections to update after resolution.")

    # 3) Single batched alterar call.
    # IMPORTANT: ensure_ascii=True — the SEI EncodingMiddleware transcodes
    # the form body to ISO-8859-1 BEFORE json_decode runs; non-ASCII bytes
    # break json_decode (PHP expects strict UTF-8). With ensure_ascii=True
    # every non-ASCII char becomes a \uXXXX escape (pure ASCII), surviving
    # the transcoding. The route itself converts back to ISO-8859-1 later
    # via mb_convert_encoding.
    alterar_form = {
        "documento": documento,
        "versao": versao,
        "secoes": json.dumps(payload, ensure_ascii=True),
    }
    alterar_body = await client.request(
        "POST",
        "/documento/secao/alterar",
        data=alterar_form,
        unidade_override=unidade_override,
    )
    return {
        "idDocumento": documento,
        "versaoAnterior": versao,
        "novaVersao": _extract_data(alterar_body),
        "secoesAtualizadas": len(changed),
        "ids": list(changed.keys()),
    }
