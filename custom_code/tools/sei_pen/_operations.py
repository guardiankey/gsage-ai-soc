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
        # NOTE: the WSSEI endpoint only honours these query params. ``tipo`` is a
        # search-mode flag ('R' = received, 'G' = generated), NOT a process-type
        # filter; ``usuario`` filters by the assignment user ID. There is no
        # free-text filter on this endpoint.
        "path": "/processo/listar",
        "path_params": [],
        "query_params": ["limit", "start", "id", "usuario", "tipo", "apenasMeus"],
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
    # Hipótese Legal
    "hipotese_legal.pesquisar": {
        "method": "GET",
        "path": "/hipoteseLegal/pesquisar",
        "path_params": [],
        "query_params": ["limit", "start", "filter", "id", "nivelAcesso"],
        "form_params": [],
        "required": ["nivelAcesso"],
    },
    # ── Reference / lookup discovery ─────────────────────────────────────────
    # Process types (id + name). Resolve ``tipoProcesso`` by name.
    "processo.tipo_listar": {
        "method": "GET",
        "path": "/processo/tipo/listar",
        "path_params": [],
        "query_params": ["id", "filter", "favoritos", "start", "limit"],
        "form_params": [],
        "required": [],
    },
    # Document types / séries (id + name). Resolve ``idSerie`` by name.
    "documento.tipo_pesquisar": {
        "method": "GET",
        "path": "/documento/tipo/pesquisar",
        "path_params": [],
        "query_params": ["id", "filter", "favoritos", "aplicabilidade", "start", "limit"],
        "form_params": [],
        "required": [],
    },
    # Séries available for external documents.
    "serie.externo_pesquisar": {
        "method": "GET",
        "path": "/serie/externo/pesquisar",
        "path_params": [],
        "query_params": ["limit", "start", "id", "filter"],
        "form_params": [],
        "required": [],
    },
    # Suggested subjects for a process type.
    "processo.assunto_sugestao": {
        "method": "GET",
        "path": "/processo/assunto/sugestao/{tipoProcedimento}/listar",
        "path_params": ["tipoProcedimento"],
        "query_params": ["limit", "start", "id", "filter"],
        "form_params": [],
        "required": ["tipoProcedimento"],
    },
    # Suggested subjects for a série.
    "documento.assunto_sugestao": {
        "method": "GET",
        "path": "/documento/assunto/sugestao/{serie}/listar",
        "path_params": ["serie"],
        "query_params": ["limit", "start", "id", "filter"],
        "form_params": [],
        "required": ["serie"],
    },
    # Interested parties / recipients lookup.
    "contato.pesquisar": {
        "method": "GET",
        "path": "/contato/pesquisar",
        "path_params": [],
        "query_params": ["filter", "idGrupoContato", "id", "limit", "start"],
        "form_params": [],
        "required": [],
    },
    # ── Document content prerequisites ───────────────────────────────────────
    # List a document's sections + last version (needed before writing content).
    # ``id`` is the internal document ID.
    "documento.secao_listar": {
        "method": "GET",
        "path": "/documento/secao/listar",
        "path_params": [],
        "query_params": ["id"],
        "form_params": [],
        "required": ["id"],
    },
    # ── Process history / movement ───────────────────────────────────────────
    # Andamentos of a process (id, usuário, data, hora, unidade, informação).
    # Source for "tempo parado na caixa" (idle-time) computation.
    "atividade.listar": {
        "method": "GET",
        "path": "/atividade/listar",
        "path_params": [],
        "query_params": ["procedimento", "limit", "start"],
        "form_params": [],
        "required": ["procedimento"],
    },
    # Related processes.
    "processo.relacionamentos": {
        "method": "GET",
        "path": "/processo/{protocolo}/relacionamentos",
        "path_params": ["protocolo"],
        "query_params": [],
        "form_params": [],
        "required": ["protocolo"],
    },
    # Interested parties attached to a process.
    "processo.interessados_listar": {
        "method": "GET",
        "path": "/processo/{protocolo}/interessados/listar",
        "path_params": ["protocolo"],
        "query_params": ["limit", "start"],
        "form_params": [],
        "required": ["protocolo"],
    },
    # Units where the process is/was open.
    "processo.unidades_listar": {
        "method": "GET",
        "path": "/processo/listar/unidades/{protocolo}",
        "path_params": ["protocolo"],
        "query_params": [],
        "form_params": [],
        "required": ["protocolo"],
    },
    # Acknowledgement (ciência) history of a process.
    "processo.ciencia_listar": {
        "method": "GET",
        "path": "/processo/{protocolo}/ciencia/listar",
        "path_params": ["protocolo"],
        "query_params": ["limit", "start"],
        "form_params": [],
        "required": ["protocolo"],
    },
    # Suspensions (sobrestamentos) of a process.
    "processo.sobrestamento_listar": {
        "method": "GET",
        "path": "/processo/listar/sobrestamento/{protocolo}",
        "path_params": ["protocolo"],
        "query_params": ["unidade"],
        "form_params": [],
        "required": ["protocolo"],
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
            "idUnidadeGeradoraProtocolo",
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
            "idUnidadeGeradoraProtocolo",
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
        # grauSigilo is only meaningful for sigiloso (nivelAcesso=2); the server
        # treats an absent value as empty, so it must not be required here.
        "required": ["tipoProcesso", "nivelAcesso", "hipoteseLegal"],
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
        # grauSigilo only matters for sigiloso (nivelAcesso=2); server defaults
        # absent to empty, so it is not strictly required.
        "required": ["protocolo", "idTipoProcesso", "nivelAcesso"],
    },
    # ── Document content ─────────────────────────────────────────────────────
    # Write a document body. ``secoes`` is a JSON-encoded string of
    # ``[{"id", "idSecaoModelo", "conteudo"}]`` (the WSSEI module json_decodes it).
    "documento.secao_alterar": {
        "method": "POST",
        "path": "/documento/secao/alterar",
        "path_params": [],
        "query_params": [],
        "form_params": ["documento", "versao", "secoes"],
        "required": ["documento", "versao", "secoes"],
    },
    # ── Process tramitation / lifecycle ──────────────────────────────────────
    # Send a process to one or more units. ``unidadesDestino`` is a CSV of unit IDs.
    "processo.enviar": {
        "method": "POST",
        "path": "/processo/enviar",
        "path_params": [],
        "query_params": [],
        "form_params": [
            "numeroProcesso", "unidadesDestino", "sinManterAbertoUnidade",
            "sinRemoverAnotacao", "sinEnviarEmailNotificacao",
            "dataRetornoProgramado", "diasRetornoProgramado",
            "sinDiasUteisRetornoProgramado", "sinReabrir",
        ],
        "required": ["numeroProcesso", "unidadesDestino"],
    },
    # Conclude a process in the current unit.
    "processo.concluir": {
        "method": "POST",
        "path": "/processo/concluir",
        "path_params": [],
        "query_params": [],
        "form_params": ["numeroProcesso"],
        "required": ["numeroProcesso"],
    },
    # Reopen a concluded process.
    "processo.reabrir": {
        "method": "POST",
        "path": "/processo/reabrir/{procedimento}",
        "path_params": ["procedimento"],
        "query_params": [],
        "form_params": [],
        "required": ["procedimento"],
    },
    # Assign a process to a user in the current unit.
    "processo.atribuir": {
        "method": "POST",
        "path": "/processo/atribuir",
        "path_params": [],
        "query_params": [],
        "form_params": ["numeroProcesso", "usuario"],
        "required": ["numeroProcesso", "usuario"],
    },
    # Remove the assignment of a process.
    "processo.remover_atribuicao": {
        "method": "POST",
        "path": "/processo/{protocolo}/remover/atribuicao",
        "path_params": ["protocolo"],
        "query_params": [],
        "form_params": [],
        "required": ["protocolo"],
    },
    # Schedule a programmed return (deadline) for a process.
    # ``dtProgramada`` is dd/MM/yyyy.
    "processo.agendar_retorno": {
        "method": "POST",
        "path": "/processo/agendar/retorno/programado",
        "path_params": [],
        "query_params": [],
        "form_params": ["unidade", "dtProgramada", "usuario", "atividadeEnvio"],
        "required": ["unidade", "dtProgramada"],
    },
    # Suspend (sobrestar) a process, optionally relating it to another.
    "processo.sobrestar": {
        "method": "POST",
        "path": "/processo/{protocolo}/sobrestar/processo",
        "path_params": ["protocolo"],
        "query_params": [],
        "form_params": ["protocoloDestino", "motivo"],
        "required": ["protocolo"],
    },
    # Cancel a process suspension.
    "processo.cancelar_sobrestamento": {
        "method": "POST",
        "path": "/processo/{protocolo}/cancelar/sobrestamento",
        "path_params": ["protocolo"],
        "query_params": [],
        "form_params": [],
        "required": ["protocolo"],
    },
    # Create an interested party (contato).
    "contato.criar": {
        "method": "POST",
        "path": "/contato/criar",
        "path_params": [],
        "query_params": [],
        "form_params": ["nome"],
        "required": ["nome"],
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
