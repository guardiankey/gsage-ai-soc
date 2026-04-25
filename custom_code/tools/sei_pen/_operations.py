"""SEI-PEN WSSEI v2 — operation definitions and request builder.

Each entry in ``READ_OPERATIONS`` and ``WRITE_OPERATIONS`` describes an API
endpoint: its HTTP method, URL path template, which params fill path segments,
which params go to query-string (GET) or form body (POST), and which params
are required.

The ``build_request`` function validates params against the operation definition
and returns the four-tuple ``(method, resolved_path, query_dict, form_dict)``
ready to be passed to :meth:`SeiPenClient.request`.
"""

from __future__ import annotations

from typing import Any, TypedDict


class OperationDef(TypedDict):
    method: str              # "GET" or "POST"
    path: str                # URL path template with {var} placeholders
    path_params: list[str]   # param names that fill path placeholders
    query_params: list[str]  # optional query-string param names (GET only)
    form_params: list[str]   # optional form-body param names (POST only)
    required: list[str]      # required param names (path + extra body/query)


# ── Read operations ───────────────────────────────────────────────────────────

READ_OPERATIONS: dict[str, OperationDef] = {
    # Órgão
    "orgao.listar": {
        "method": "GET",
        "path": "/orgao/listar",
        "path_params": [],
        "query_params": [],
        "form_params": [],
        "required": [],
    },
    # Documento
    "documento.visualizar": {
        "method": "GET",
        "path": "/documento/{documento}/interno/visualizar",
        "path_params": ["documento"],
        "query_params": [],
        "form_params": [],
        "required": ["documento"],
    },
    "documento.consultar_interno": {
        "method": "GET",
        "path": "/documento/interno/consultar/{protocolo}",
        "path_params": ["protocolo"],
        "query_params": [],
        "form_params": [],
        "required": ["protocolo"],
    },
    "documento.listar_em_processo": {
        "method": "GET",
        "path": "/documento/listar/{procedimento}",
        "path_params": ["procedimento"],
        "query_params": ["limit", "start"],
        "form_params": [],
        "required": ["procedimento"],
    },
    # Unidade
    "unidade.pesquisar": {
        "method": "GET",
        "path": "/unidade/pesquisar",
        "path_params": [],
        "query_params": ["limit", "start", "filter"],
        "form_params": [],
        "required": [],
    },
    "unidade.pesquisar_outras": {
        "method": "GET",
        "path": "/unidade/outras/pesquisar",
        "path_params": [],
        "query_params": ["limit", "start", "filter", "id"],
        "form_params": [],
        "required": [],
    },
    "unidade.pesquisar_texto_padrao": {
        "method": "GET",
        "path": "/unidade/textopadrao/interno/pesquisar",
        "path_params": [],
        "query_params": ["limit", "start", "filter", "id"],
        "form_params": [],
        "required": [],
    },
    # Processo — assunto
    "processo.pesquisar_assunto": {
        "method": "GET",
        "path": "/processo/assunto/pesquisar",
        "path_params": [],
        "query_params": ["limit", "start", "filter", "id"],
        "form_params": [],
        "required": [],
    },
    # Processo — acompanhamentos
    "processo.listar_meus_acompanhamentos": {
        "method": "GET",
        "path": "/processo/listar/meus/acompanhamentos",
        "path_params": [],
        "query_params": ["limit", "start", "grupo", "usuario"],
        "form_params": [],
        "required": [],
    },
    "processo.listar_acompanhamentos": {
        "method": "GET",
        "path": "/processo/listar/acompanhamentos",
        "path_params": [],
        "query_params": ["limit", "start", "grupo"],
        "form_params": [],
        "required": ["grupo"],
    },
    # Processo — pesquisa
    "processo.pesquisar_geral": {
        "method": "GET",
        "path": "/processo/pesquisar",
        "path_params": [],
        "query_params": [
            "limit", "start", "grupo", "palavrasChave", "descricao",
            "staTipoData", "dataInicio", "dataFim", "idUnidadeGeradora",
            "idAssunto", "buscaRapida",
        ],
        "form_params": [],
        "required": [],
    },
    "processo.listar": {
        "method": "GET",
        "path": "/processo/listar",
        "path_params": [],
        "query_params": ["limit", "start", "filter", "id", "usuario", "tipo", "apenasMeus"],
        "form_params": [],
        "required": [],
    },
    # Processo — consulta
    "processo.consultar": {
        "method": "GET",
        "path": "/processo/{protocolo}",
        "path_params": ["protocolo"],
        "query_params": [],
        "form_params": [],
        "required": ["protocolo"],
    },
    "processo.consultar_atribuicao": {
        "method": "GET",
        "path": "/processo/{protocolo}/consultar/atribuicao",
        "path_params": ["protocolo"],
        "query_params": [],
        "form_params": [],
        "required": ["protocolo"],
    },
    "processo.consultar_acompanhamento": {
        "method": "GET",
        "path": "/processo/acompanhamento/consultar",
        "path_params": [],
        "query_params": ["protocolo"],
        "form_params": [],
        "required": ["protocolo"],
    },
    # Grupo de Acompanhamento
    "grupo_acompanhamento.listar": {
        "method": "GET",
        "path": "/grupoacompanhamento/listar",
        "path_params": [],
        "query_params": ["limit", "start", "filter", "id"],
        "form_params": [],
        "required": [],
    },
    # Modelo de Documento
    "modelo_documento.listar_grupo": {
        "method": "GET",
        "path": "/protocolomodelo/grupo/listar",
        "path_params": [],
        "query_params": ["limit", "start", "filter", "id"],
        "form_params": [],
        "required": [],
    },
    "modelo_documento.listar": {
        "method": "GET",
        "path": "/protocolomodelo/listar",
        "path_params": [],
        "query_params": ["limit", "start", "filter", "id", "grupoProtocoloModelo", "tipoFiltro"],
        "form_params": [],
        "required": [],
    },
    # Acompanhamento Especial
    "acompanhamento_especial.listar": {
        "method": "GET",
        "path": "/acompanhamentoespecial/listar",
        "path_params": [],
        "query_params": ["limit", "start", "grupoAcompanhamento"],
        "form_params": [],
        "required": [],
    },
    # Usuário
    "usuario.pesquisar": {
        "method": "GET",
        "path": "/usuario/pesquisar",
        "path_params": [],
        "query_params": ["palavrachave", "orgao"],
        "form_params": [],
        "required": ["palavrachave"],
    },
    "usuario.listar_unidades": {
        "method": "GET",
        "path": "/usuario/unidades",
        "path_params": [],
        "query_params": ["usuario"],
        "form_params": [],
        "required": ["usuario"],
    },
}

# ── Write operations ──────────────────────────────────────────────────────────

WRITE_OPERATIONS: dict[str, OperationDef] = {
    # Documento — dar ciência
    "documento.dar_ciencia": {
        "method": "POST",
        "path": "/documento/ciencia",
        "path_params": [],
        "query_params": [],
        "form_params": ["documento"],
        "required": ["documento"],
    },
    # Documento — cadastrar interno
    "documento.cadastrar_interno": {
        "method": "POST",
        "path": "/documento/{procedimento}/interno/criar",
        "path_params": ["procedimento"],
        "query_params": [],
        "form_params": [
            "idSerie", "observacao", "nivelAcesso",
            "assuntos", "interessados", "idHipoteseLegal",
            "protocoloDocumentoModelo", "descricao", "destinatarios",
        ],
        "required": ["procedimento", "idSerie", "observacao", "nivelAcesso"],
    },
    # Documento — alterar interno
    "documento.alterar_interno": {
        "method": "POST",
        "path": "/documento/interno/{documento}/alterar",
        "path_params": ["documento"],
        "query_params": [],
        "form_params": [
            "observacao", "nivelAcesso",
            "assuntos", "interessados", "idHipoteseLegal",
            "descricao", "destinatarios",
        ],
        "required": ["documento", "observacao", "nivelAcesso"],
    },
    # Processo — criar
    "processo.criar": {
        "method": "POST",
        "path": "/processo/criar",
        "path_params": [],
        "query_params": [],
        "form_params": [
            "tipoProcesso", "nivelAcesso", "hipoteseLegal", "grauSigilo",
            "assuntos", "interessados", "especificacao", "observacoes",
        ],
        "required": ["tipoProcesso", "nivelAcesso", "hipoteseLegal", "grauSigilo"],
    },
    # Processo — alterar
    "processo.alterar": {
        "method": "POST",
        "path": "/processo/{protocolo}/alterar",
        "path_params": ["protocolo"],
        "query_params": [],
        "form_params": [
            "idTipoProcesso", "nivelAcesso", "grauSigilo",
            "assuntos", "interessados", "especificacao",
            "observacao", "idHipoteseLegal",
        ],
        "required": ["protocolo", "idTipoProcesso", "nivelAcesso", "grauSigilo"],
    },
}

# ── Request builder ───────────────────────────────────────────────────────────


class BuildError(Exception):
    """Raised when required params are missing for a given operation."""


def build_request(
    operation: str,
    params: dict[str, Any],
    *,
    is_write: bool = False,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Validate *params* and build the four-tuple for :meth:`SeiPenClient.request`.

    Parameters
    ----------
    operation :
        Operation identifier, e.g. ``"processo.consultar"``.
    params :
        Raw params dict from the tool call.
    is_write :
        ``True`` to look up in ``WRITE_OPERATIONS``; ``False`` for reads.

    Returns
    -------
    tuple[str, str, dict, dict]
        ``(method, resolved_path, query_params, form_data)``

    Raises
    ------
    BuildError
        If the operation is unknown or required params are missing.
    """
    registry = WRITE_OPERATIONS if is_write else READ_OPERATIONS
    if operation not in registry:
        valid = ", ".join(sorted(registry.keys()))
        raise BuildError(
            f"Unknown operation '{operation}'. Valid operations: {valid}"
        )

    op = registry[operation]
    method = op["method"]
    path = op["path"]

    # ── Validate and substitute path params ──────────────────────────────────
    for pp in op["path_params"]:
        value = params.get(pp)
        if not value and value != 0:
            raise BuildError(
                f"Required path param '{pp}' is missing for operation '{operation}'"
            )
        path = path.replace("{" + pp + "}", str(value))

    # ── Validate non-path required params ────────────────────────────────────
    for req in op["required"]:
        if req in op["path_params"]:
            continue  # already validated above
        value = params.get(req)
        if value is None or value == "":
            raise BuildError(
                f"Required param '{req}' is missing for operation '{operation}'"
            )

    # ── Build query-string dict ───────────────────────────────────────────────
    query: dict[str, Any] = {
        k: params[k]
        for k in op["query_params"]
        if k in params and params[k] is not None and params[k] != ""
    }

    # ── Build form-body dict ──────────────────────────────────────────────────
    form: dict[str, Any] = {
        k: params[k]
        for k in op["form_params"]
        if k in params and params[k] is not None and params[k] != ""
    }

    return method, path, query, form
