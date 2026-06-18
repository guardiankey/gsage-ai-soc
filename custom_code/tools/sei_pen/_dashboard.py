"""SEI-PEN managerial dashboard helpers.

Each helper issues bounded WSSEI queries and returns a structured dict ready to
be emitted by the ``sei_pen_dashboard`` tool:

    {"summary": {...}, "rows": [...], "mermaid": "<diagram or ''>", "truncated": bool}

To keep latency and payload predictable every helper imposes a hard cap on the
number of processes/documents fetched and surfaces a ``truncated`` flag.

Data shapes (from the WSSEI v2 source, ``limbo/mod-wssei``):

* ``GET /processo/listar`` row:
  ``{id, status, atributos:{idProcedimento, numero, tipoProcesso, descricao,
  unidade:{idUnidade, sigla}, status:{retornoProgramado, retornoAtrasado,
  processoSobrestado, ...}}}``
* ``GET /documento/listar/{procedimento}`` row:
  ``{id, atributos:{titulo, tipo (=série name), protocoloFormatado, ...}}``
* ``GET /atividade/listar?procedimento=`` row (andamento):
  ``{id, atributos:{idProcesso, usuario, data (dd/MM/yyyy), hora, unidade,
  informacao}}``
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Any, Optional

from custom_code.tools.sei_pen._client import SeiPenClient, SeiPenError

log = logging.getLogger(__name__)

# Hard caps to keep dashboards bounded.
_MAX_PROCESSES = 200
_MAX_ACTIVITY_PROBES = 60      # processes probed for idle-time (extra API calls)
_MAX_DOCS = 500
_MAX_ROWS = 100
_MAX_GROUPS = 30              # tracking groups probed in the acompanhamentos fallback

# Idle-time buckets (days).
_BUCKET_FRESH = "< 7d"
_BUCKET_MID = "7–30d"
_BUCKET_STALE = "> 30d"


def _as_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        return [data]
    return []


def _attr(row: dict) -> dict:
    attr = row.get("atributos")
    return attr if isinstance(attr, dict) else {}


def _parse_sei_date(raw: Any) -> Optional[datetime]:
    """Parse a SEI date string (dd/MM/yyyy or dd/MM/yyyy HH:MM) → datetime."""
    if not raw:
        return None
    s = str(raw).strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _pie(title: str, counts: dict[str, int]) -> str:
    """Build a Mermaid pie chart; empty string when there is nothing to show."""
    items = [(k, v) for k, v in counts.items() if v > 0]
    if not items:
        return ""
    lines = ["pie showData", f'    title {title}']
    for label, value in items:
        safe = str(label).replace('"', "'")
        lines.append(f'    "{safe}" : {value}')
    return "\n".join(lines)


def _idle_bucket(days: int) -> str:
    if days < 7:
        return _BUCKET_FRESH
    if days <= 30:
        return _BUCKET_MID
    return _BUCKET_STALE


async def _resolve_unidade_info(
    client: SeiPenClient,
    unidade_id: str,
    *,
    unidade_override: Optional[str] = None,
) -> dict:
    """Resolve unit name (sigla) for a given unit ID.

    Tries two endpoints in order:
    1. ``GET /unidade/pesquisar?filter=<id>`` — text search by ID.
    2. ``GET /unidade/pesquisar_outras?id=<id>`` — explicit ID lookup.

    Falls back to the raw ID when the name is unresolvable.
    """
    target = unidade_override or unidade_id
    if not target:
        return {"id": "", "nome": ""}

    async def _try_lookup(path: str, query: dict) -> Optional[str]:
        try:
            body = await client.request(
                "GET", path, params=query,
                unidade_override=unidade_override,
            )
            unidades = _as_list(body.get("data"))
            for u in unidades:
                uid = str(u.get("idUnidade") or u.get("id") or "")
                if uid == target:
                    return str(u.get("sigla") or u.get("descricao") or u.get("nome") or "")
            return None
        except SeiPenError:
            return None

    # 1) Text search (finds by substring match on sigla/descricao).
    nome = await _try_lookup(
        "/unidade/pesquisar",
        {"filter": target, "limit": 10, "start": 0},
    )
    if nome:
        return {"id": target, "nome": nome}

    # 2) Explicit ID lookup (``id`` query param on the "outras" endpoint).
    nome = await _try_lookup(
        "/unidade/outras/pesquisar",
        {"id": target, "limit": 5, "start": 0},
    )
    if nome:
        return {"id": target, "nome": nome}

    # Not found — fall back to the raw ID.
    return {"id": target, "nome": target}


async def _list_meus_processos(
    client: SeiPenClient,
    *,
    unidade_override: Optional[str],
    limit: int,
    tipo: Optional[str] = "G",
    apenas_meus: bool = False,
) -> tuple[list[dict], bool]:
    """Fetch the caller's open processes, capped.

    Defaults to ``tipo='G'`` (generated processes) to match the most common
    SEI usage where "my processes" means processes created by the unit.
    ``apenas_meus=True`` switches to the stricter "assigned to me" filter.
    """
    params: dict[str, Any] = {"limit": limit, "start": 0}
    if tipo:
        params["tipo"] = tipo
    if apenas_meus:
        params["apenasMeus"] = "S"
    body = await client.request(
        "GET",
        "/processo/listar",
        params=params,
        unidade_override=unidade_override,
    )
    rows = _as_list(body.get("data"))
    truncated = len(rows) >= limit
    return rows[:limit], truncated


# ── Views ─────────────────────────────────────────────────────────────────────


async def meus_processos(
    client: SeiPenClient,
    *,
    unidade_override: Optional[str] = None,
    tipo: Optional[str] = "G",
) -> dict:
    """Open processes of the caller in the current unit."""
    rows, truncated = await _list_meus_processos(
        client, unidade_override=unidade_override, limit=_MAX_PROCESSES, tipo=tipo
    )

    sobrestados = retorno_prog = retorno_atraso = 0
    out_rows: list[dict] = []
    for r in rows:
        attr = _attr(r)
        raw_status = attr.get("status")
        status: dict = raw_status if isinstance(raw_status, dict) else {}
        if str(status.get("processoSobrestado", "N")).upper() == "S":
            sobrestados += 1
        if str(status.get("retornoProgramado", "N")).upper() == "S":
            retorno_prog += 1
        if str(status.get("retornoAtrasado", "N")).upper() == "S":
            retorno_atraso += 1
        out_rows.append(
            {
                "numero": attr.get("numero"),
                "tipoProcesso": attr.get("tipoProcesso"),
                "descricao": attr.get("descricao"),
                "unidade": (attr.get("unidade") or {}).get("sigla"),
                "sobrestado": str(status.get("processoSobrestado", "N")).upper() == "S",
                "retornoAtrasado": str(status.get("retornoAtrasado", "N")).upper() == "S",
            }
        )

    total = len(rows)
    summary = {
        "total": total,
        "sobrestados": sobrestados,
        "retorno_programado": retorno_prog,
        "retorno_atrasado": retorno_atraso,
        "normais": total - sobrestados - retorno_atraso,
    }
    mermaid = _pie(
        "Meus processos por situação",
        {
            "Atrasado": retorno_atraso,
            "Sobrestado": sobrestados,
            "Retorno programado": retorno_prog,
            "Normal": max(total - sobrestados - retorno_atraso - retorno_prog, 0),
        },
    )
    return {
        "summary": summary,
        "rows": out_rows[:_MAX_ROWS],
        "mermaid": mermaid,
        "truncated": truncated or len(out_rows) > _MAX_ROWS,
    }


async def prazos(
    client: SeiPenClient,
    *,
    unidade_override: Optional[str] = None,
    top_n: int = 20,
    tipo: Optional[str] = "G",
) -> dict:
    """Idle time per process ("tempo parado na caixa").

    For each open process (capped at ``_MAX_ACTIVITY_PROBES``) the latest
    recorded activity date is read from ``GET /atividade/listar`` and the idle
    days are computed as ``today − last activity date``.
    """
    rows, truncated = await _list_meus_processos(
        client, unidade_override=unidade_override, limit=_MAX_PROCESSES, tipo=tipo
    )
    probe = rows[:_MAX_ACTIVITY_PROBES]
    truncated = truncated or len(rows) > _MAX_ACTIVITY_PROBES

    now = datetime.now()
    buckets: Counter[str] = Counter()
    measured: list[dict] = []
    for r in probe:
        attr = _attr(r)
        procedimento = attr.get("idProcedimento") or r.get("id")
        if not procedimento:
            continue
        try:
            act_body = await client.request(
                "GET",
                "/atividade/listar",
                params={"procedimento": procedimento, "limit": 50, "start": 0},
                unidade_override=unidade_override,
            )
        except SeiPenError:
            continue
        andamentos = _as_list(act_body.get("data"))
        latest: Optional[datetime] = None
        for a in andamentos:
            d = _parse_sei_date(_attr(a).get("data"))
            if d and (latest is None or d > latest):
                latest = d
        if latest is None:
            continue
        idle_days = max((now - latest).days, 0)
        buckets[_idle_bucket(idle_days)] += 1
        measured.append(
            {
                "numero": attr.get("numero"),
                "tipoProcesso": attr.get("tipoProcesso"),
                "ultimaAtividade": latest.strftime("%d/%m/%Y"),
                "diasParado": idle_days,
            }
        )

    measured.sort(key=lambda m: m["diasParado"], reverse=True)
    summary = {
        "processos_avaliados": len(measured),
        "buckets": {
            _BUCKET_FRESH: buckets[_BUCKET_FRESH],
            _BUCKET_MID: buckets[_BUCKET_MID],
            _BUCKET_STALE: buckets[_BUCKET_STALE],
        },
        "mais_parado": measured[0] if measured else None,
    }
    mermaid = _pie(
        "Processos por tempo parado",
        {
            _BUCKET_FRESH: buckets[_BUCKET_FRESH],
            _BUCKET_MID: buckets[_BUCKET_MID],
            _BUCKET_STALE: buckets[_BUCKET_STALE],
        },
    )
    return {
        "summary": summary,
        "rows": measured[:top_n],
        "mermaid": mermaid,
        "truncated": truncated,
    }


async def processos_por_tipo(
    client: SeiPenClient,
    *,
    unidade_override: Optional[str] = None,
    tipo: Optional[str] = "G",
) -> dict:
    """Distribution of the caller's open processes across process types."""
    rows, truncated = await _list_meus_processos(
        client, unidade_override=unidade_override, limit=_MAX_PROCESSES, tipo=tipo
    )
    counts: Counter[str] = Counter()
    for r in rows:
        tipo = _attr(r).get("tipoProcesso") or "(sem tipo)"
        counts[str(tipo)] += 1

    out_rows = [
        {"tipoProcesso": k, "total": v}
        for k, v in counts.most_common(_MAX_ROWS)
    ]
    return {
        "summary": {"total": len(rows), "tipos_distintos": len(counts)},
        "rows": out_rows,
        "mermaid": _pie("Processos por tipo", dict(counts.most_common(12))),
        "truncated": truncated,
    }


async def processos_por_assunto(
    client: SeiPenClient,
    *,
    unidade_override: Optional[str] = None,
    tipo: Optional[str] = "G",
) -> dict:
    """Distribution of the caller's open processes across subjects.

    The process listing does not carry the structured subject (assunto); the
    free-text specification (``descricao``) is used as a subject proxy.
    """
    rows, truncated = await _list_meus_processos(
        client, unidade_override=unidade_override, limit=_MAX_PROCESSES, tipo=tipo
    )
    counts: Counter[str] = Counter()
    for r in rows:
        assunto = (_attr(r).get("descricao") or "(sem especificação)").strip()
        counts[str(assunto)[:120]] += 1

    out_rows = [
        {"assunto": k, "total": v} for k, v in counts.most_common(_MAX_ROWS)
    ]
    return {
        "summary": {
            "total": len(rows),
            "assuntos_distintos": len(counts),
            "nota": "Agrupado pela especificação (descricao); a API de listagem "
            "não retorna o assunto estruturado.",
        },
        "rows": out_rows,
        "mermaid": _pie("Processos por assunto", dict(counts.most_common(12))),
        "truncated": truncated,
    }


async def documentos_por_processo(
    client: SeiPenClient,
    *,
    procedimento: str,
    unidade_override: Optional[str] = None,
) -> dict:
    """Documents of a process grouped by document type (série)."""
    if not procedimento:
        raise SeiPenError("documentos_por_processo requires 'procedimento'.")
    body = await client.request(
        "GET",
        f"/documento/listar/{procedimento}",
        params={"limit": _MAX_DOCS, "start": 0},
        unidade_override=unidade_override,
    )
    docs = _as_list(body.get("data"))
    truncated = len(docs) >= _MAX_DOCS
    counts: Counter[str] = Counter()
    for d in docs:
        serie = _attr(d).get("tipo") or "(sem tipo)"
        counts[str(serie)] += 1

    out_rows = [
        {"serie": k, "total": v} for k, v in counts.most_common(_MAX_ROWS)
    ]
    return {
        "summary": {
            "procedimento": procedimento,
            "total_documentos": len(docs),
            "series_distintas": len(counts),
        },
        "rows": out_rows,
        "mermaid": _pie(
            f"Documentos do processo {procedimento} por série",
            dict(counts.most_common(12)),
        ),
        "truncated": truncated,
    }


async def _acompanhamentos_por_grupo(
    client: SeiPenClient, *, unidade_override: Optional[str]
) -> tuple[Counter[str], int, bool]:
    """Count tracked processes per group using the per-group endpoint.

    ``/processo/listar/acompanhamentos?grupo=X`` resolves through
    ``AcompanhamentoRN->listarAcompanhamentosUnidade`` on the server, a code
    path that does NOT touch the buggy ``acompanhamento.id_usuario_gerador``
    column used by ``/processo/listar/meus/acompanhamentos``. We enumerate the
    tracking groups, then tally each group's processes.
    """
    grp_body = await client.request(
        "GET",
        "/grupoacompanhamento/listar",
        params={"limit": _MAX_ROWS, "start": 0},
        unidade_override=unidade_override,
    )
    grupos = _as_list(grp_body.get("data"))

    counts: Counter[str] = Counter()
    total = 0
    truncated = len(grupos) >= _MAX_ROWS
    for g in grupos[:_MAX_GROUPS]:
        grupo_id = g.get("idGrupoAcompanhamento") or g.get("id")
        nome = str(g.get("nome") or grupo_id or "(sem grupo)")
        if not grupo_id:
            continue
        proc_body = await client.request(
            "GET",
            "/processo/listar/acompanhamentos",
            params={"grupo": grupo_id, "limit": _MAX_PROCESSES, "start": 0},
            unidade_override=unidade_override,
        )
        procs = _as_list(proc_body.get("data"))
        counts[nome] += len(procs)
        total += len(procs)
    if len(grupos) > _MAX_GROUPS:
        truncated = True
    return counts, total, truncated


async def acompanhamentos(
    client: SeiPenClient, *, unidade_override: Optional[str] = None
) -> dict:
    """Tracked processes grouped by tracking group.

    Prefers the per-user endpoint ``/processo/listar/meus/acompanhamentos``;
    when that fails (a known server-side SQL bug on some installations) it
    falls back to enumerating tracking groups and tallying each group's
    processes via the per-group endpoint, which uses an unaffected code path.
    Only if both fail does the view degrade to an instructive note.
    """
    fonte = "meus_acompanhamentos"
    try:
        body = await client.request(
            "GET",
            "/processo/listar/meus/acompanhamentos",
            params={"limit": _MAX_PROCESSES, "start": 0},
            unidade_override=unidade_override,
        )
        rows = _as_list(body.get("data"))
        counts: Counter[str] = Counter()
        for r in rows:
            attr = _attr(r)
            grupo = attr.get("grupo") or attr.get("nomeGrupo") or "(sem grupo)"
            counts[str(grupo)] += 1
        total = len(rows)
        truncated = len(rows) >= _MAX_PROCESSES
    except SeiPenError as exc:
        log.info(
            "sei_pen_dashboard: meus/acompanhamentos failed (%s); "
            "falling back to per-group enumeration",
            exc,
        )
        try:
            counts, total, truncated = await _acompanhamentos_por_grupo(
                client, unidade_override=unidade_override
            )
            fonte = "por_grupo_fallback"
        except SeiPenError as exc2:
            return {
                "summary": {
                    "disponivel": False,
                    "nota": (
                        "Endpoint de acompanhamentos indisponível nesta "
                        f"instalação (possível bug do servidor WSSEI): {exc2}"
                    ),
                },
                "rows": [],
                "mermaid": "",
                "truncated": False,
            }

    out_rows = [
        {"grupo": k, "total": v} for k, v in counts.most_common(_MAX_ROWS)
    ]
    return {
        "summary": {
            "disponivel": True,
            "fonte": fonte,
            "total": total,
            "grupos": len(counts),
        },
        "rows": out_rows,
        "mermaid": _pie("Acompanhamentos por grupo", dict(counts.most_common(12))),
        "truncated": truncated,
    }
