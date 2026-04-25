"""gSage AI — SEI SOAP webservice client.

Provides an async wrapper over the SEI (Sistema Eletrônico de Informações)
SOAP webservice using zeep.

Authentication
--------------
Every SEI operation receives SiglaSistema + IdentificacaoServico.
These are configured via constructor arguments or environment variables:

  SEI_WSDL_URL                  — WSDL URL of the SEI webservice
                                  (e.g. https://sei.example.gov.br/sei/ws/SeiWS.php?wsdl)
  SEI_SIGLA_SISTEMA             — System acronym registered in SEI admin
  SEI_IDENTIFICACAO_SERVICO     — Access key (chave de acesso) for the system
  SEI_ID_UNIDADE                — Default unit ID (can be overridden per call)

Usage
-----
::

    async with SEIClient() as client:
        units = await client.listar_unidades()
        proc = await client.consultar_procedimento("35014.000001/2020-31")
"""

from __future__ import annotations

import base64
import logging
import os
from types import TracebackType
from typing import Any, Optional

import httpx
import zeep
from zeep import AsyncClient, Settings
from zeep.exceptions import Fault as ZeepFault
from zeep.helpers import serialize_object
from zeep.transports import AsyncTransport

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
# SEI WSDL namespace URI (must match targetNamespace in the WSDL)
_SEI_NS = "Sei"


class _FollowRedirectsTransport(AsyncTransport):
    """AsyncTransport that follows HTTP 3xx redirects when fetching WSDL schemas.

    By default ``httpx.Client`` (used internally by zeep's AsyncTransport for
    synchronous WSDL loading) does NOT follow redirects.  The SOAP encoding
    schema imported by SEI's WSDL (http://schemas.xmlsoap.org/soap/encoding/)
    returns a 307 → HTTPS redirect, which breaks WSDL parsing without this fix.
    """

    def _load_remote_data(self, url: str) -> bytes:  # type: ignore[override]
        log.debug("SEI: fetching schema %s", url)
        with httpx.Client(follow_redirects=True) as client:
            response = client.get(url, timeout=self.wsdl_client.timeout)
            response.raise_for_status()
            log.debug("SEI: schema OK %s (%d bytes)", url, len(response.content))
            return response.content


class SEIError(Exception):
    """Raised when the SEI SOAP webservice returns a fault or config is missing.

    Attributes
    ----------
    fault_code : str | None
        The SOAP fault code returned by SEI, if available.
    """

    def __init__(self, message: str, fault_code: Optional[str] = None) -> None:
        super().__init__(message)
        self.fault_code = fault_code


def _serialize_result(obj: Any) -> Any:
    """Convert a zeep response object into a plain Python dict/list."""
    if obj is None:
        return None
    try:
        return serialize_object(obj)
    except Exception:
        return str(obj)


class SEIClient:
    """Async SEI SOAP client built on zeep.

    Parameters
    ----------
    url :
        Full URL to the SEI WSDL endpoint.
        Falls back to ``SEI_WSDL_URL`` env var.
    sigla_sistema :
        System acronym registered in SEI admin panel.
        Falls back to ``SEI_SIGLA_SISTEMA`` env var.
    identificacao_servico :
        Access key (chave de acesso / IdentificacaoServico).
        Falls back to ``SEI_IDENTIFICACAO_SERVICO`` env var.
    id_unidade :
        Default unit ID sent with every call that requires it.
        Falls back to ``SEI_ID_UNIDADE`` env var.  Can be overridden per call.
    timeout :
        HTTP timeout for SOAP calls in seconds (default: 30).
    """

    def __init__(
        self,
        url: Optional[str] = None,
        sigla_sistema: Optional[str] = None,
        identificacao_servico: Optional[str] = None,
        id_unidade: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._url = url or os.getenv("SEI_WSDL_URL", "")
        self._sigla = sigla_sistema or os.getenv("SEI_SIGLA_SISTEMA", "")
        self._servico = identificacao_servico or os.getenv("SEI_IDENTIFICACAO_SERVICO", "")
        self._id_unidade = id_unidade or os.getenv("SEI_ID_UNIDADE", "")
        self._timeout = timeout
        self._client: Optional[AsyncClient] = None

    # ── Context manager ────────────────────────────────────────────────────

    async def __aenter__(self) -> "SEIClient":
        if not self._url:
            raise SEIError(
                "SEI_WSDL_URL is not configured.", fault_code="CONFIG_MISSING"
            )
        log.debug(
            "SEI: connecting to %s (sigla=%s unit=%s timeout=%gs)",
            self._url, self._sigla, self._id_unidade, self._timeout,
        )
        settings = Settings(strict=False, xml_huge_tree=True)  # type: ignore[call-arg]
        transport = _FollowRedirectsTransport(timeout=int(self._timeout))
        self._client = AsyncClient(self._url, settings=settings, transport=transport)
        log.debug("SEI: WSDL loaded OK")
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self._client = None

    # ── Internal helpers ───────────────────────────────────────────────────

    def _svc(self) -> Any:
        if self._client is None:
            raise SEIError("SEIClient is not initialized — use as async context manager.")
        return self._client.service

    def _auth(self) -> tuple[str, str]:
        if not self._sigla:
            raise SEIError(
                "SEI_SIGLA_SISTEMA is not configured.", fault_code="CONFIG_MISSING"
            )
        if not self._servico:
            raise SEIError(
                "SEI_IDENTIFICACAO_SERVICO is not configured.", fault_code="CONFIG_MISSING"
            )
        return self._sigla, self._servico

    def _unit(self, override: Optional[str] = None) -> str:
        return override or self._id_unidade or ""

    def _get_type(self, type_name: str) -> Any:
        """Return a zeep type factory for a SEI complex type."""
        assert self._client is not None
        return self._client.get_type(f"{{{_SEI_NS}}}{type_name}")

    # ── Read operations ────────────────────────────────────────────────────

    async def consultar_procedimento(
        self,
        protocolo: str,
        id_unidade: Optional[str] = None,
    ) -> dict:
        """Retrieve full details of a SEI process by protocol number."""
        sigla, servico = self._auth()
        log.debug("SEI: consultar_procedimento protocolo=%s unit=%s", protocolo, self._unit(id_unidade))
        try:
            result = await self._svc().consultarProcedimento(
                SiglaSistema=sigla,
                IdentificacaoServico=servico,
                IdUnidade=self._unit(id_unidade),
                ProtocoloProcedimento=protocolo,
                SinRetornarAssuntos="S",
                SinRetornarInteressados="S",
                SinRetornarObservacoes="S",
                SinRetornarAndamentoGeracao="S",
                SinRetornarAndamentoConclusao="S",
                SinRetornarUltimoAndamento="S",
                SinRetornarUnidadesProcedimentoAberto="S",
                SinRetornarProcedimentosRelacionados="S",
                SinRetornarProcedimentosAnexados="S",
            )
            return _serialize_result(result) or {}
        except ZeepFault as exc:
            raise SEIError(str(exc), fault_code=getattr(exc, "code", None)) from exc

    async def consultar_documento(
        self,
        protocolo_documento: str,
        id_unidade: Optional[str] = None,
    ) -> dict:
        """Retrieve document details by formatted protocol number."""
        sigla, servico = self._auth()
        log.debug("SEI: consultar_documento protocolo_doc=%s unit=%s", protocolo_documento, self._unit(id_unidade))
        try:
            result = await self._svc().consultarDocumento(
                SiglaSistema=sigla,
                IdentificacaoServico=servico,
                IdUnidade=self._unit(id_unidade),
                ProtocoloDocumento=protocolo_documento,
                SinRetornarAndamentoGeracao="S",
                SinRetornarAssinaturas="S",
                SinRetornarPublicacao="N",
                SinRetornarCampos="S",
            )
            return _serialize_result(result) or {}
        except ZeepFault as exc:
            raise SEIError(str(exc), fault_code=getattr(exc, "code", None)) from exc

    async def listar_unidades(
        self,
        id_tipo_procedimento: Optional[str] = None,
        id_serie: Optional[str] = None,
    ) -> list:
        """List all organizational units visible to the system."""
        sigla, servico = self._auth()
        log.debug("SEI: listar_unidades tipo_proc=%r serie=%r", id_tipo_procedimento, id_serie)
        try:
            result = await self._svc().listarUnidades(
                SiglaSistema=sigla,
                IdentificacaoServico=servico,
                IdTipoProcedimento=id_tipo_procedimento or "",
                IdSerie=id_serie or "",
            )
            return _serialize_result(result) or []
        except ZeepFault as exc:
            raise SEIError(str(exc), fault_code=getattr(exc, "code", None)) from exc

    async def listar_series(
        self,
        id_unidade: Optional[str] = None,
        id_tipo_procedimento: Optional[str] = None,
    ) -> list:
        """List document series (tipos de série) for a unit."""
        sigla, servico = self._auth()
        log.debug("SEI: listar_series unit=%s tipo_proc=%r", self._unit(id_unidade), id_tipo_procedimento)
        try:
            result = await self._svc().listarSeries(
                SiglaSistema=sigla,
                IdentificacaoServico=servico,
                IdUnidade=self._unit(id_unidade),
                IdTipoProcedimento=id_tipo_procedimento or "",
            )
            return _serialize_result(result) or []
        except ZeepFault as exc:
            raise SEIError(str(exc), fault_code=getattr(exc, "code", None)) from exc

    async def listar_tipos_procedimento(
        self,
        id_unidade: Optional[str] = None,
        id_serie: Optional[str] = None,
    ) -> list:
        """List process types available for a unit."""
        sigla, servico = self._auth()
        log.debug("SEI: listar_tipos_procedimento unit=%s serie=%r", self._unit(id_unidade), id_serie)
        try:
            result = await self._svc().listarTiposProcedimento(
                SiglaSistema=sigla,
                IdentificacaoServico=servico,
                IdUnidade=self._unit(id_unidade),
                IdSerie=id_serie or "",
            )
            return _serialize_result(result) or []
        except ZeepFault as exc:
            raise SEIError(str(exc), fault_code=getattr(exc, "code", None)) from exc

    async def listar_usuarios(
        self,
        id_unidade: Optional[str] = None,
        id_usuario: Optional[str] = None,
    ) -> list:
        """List users of a given unit."""
        sigla, servico = self._auth()
        log.debug("SEI: listar_usuarios unit=%s id_usuario=%r", self._unit(id_unidade), id_usuario)
        try:
            result = await self._svc().listarUsuarios(
                SiglaSistema=sigla,
                IdentificacaoServico=servico,
                IdUnidade=self._unit(id_unidade),
                IdUsuario=id_usuario or "",
            )
            return _serialize_result(result) or []
        except ZeepFault as exc:
            raise SEIError(str(exc), fault_code=getattr(exc, "code", None)) from exc

    async def listar_andamentos(
        self,
        protocolo: str,
        id_unidade: Optional[str] = None,
    ) -> list:
        """List history entries (andamentos) for a process."""
        sigla, servico = self._auth()
        log.debug("SEI: listar_andamentos protocolo=%s unit=%s", protocolo, self._unit(id_unidade))
        try:
            result = await self._svc().listarAndamentos(
                SiglaSistema=sigla,
                IdentificacaoServico=servico,
                IdUnidade=self._unit(id_unidade),
                ProtocoloProcedimento=protocolo,
                SinRetornarAtributos="S",
                Andamentos=None,
                Tarefas=None,
                TarefasModulos=None,
            )
            return _serialize_result(result) or []
        except ZeepFault as exc:
            raise SEIError(str(exc), fault_code=getattr(exc, "code", None)) from exc

    # ── Write operations ───────────────────────────────────────────────────

    async def gerar_procedimento(
        self,
        id_tipo_procedimento: str,
        especificacao: str,
        nivel_acesso: str = "0",
        assuntos: Optional[list[dict]] = None,
        interessados: Optional[list[dict]] = None,
        observacao: Optional[str] = None,
        id_hipotese_legal: Optional[str] = None,
        id_unidade: Optional[str] = None,
    ) -> dict:
        """Create a new SEI process (procedimento).

        Parameters
        ----------
        id_tipo_procedimento : str
            SEI process type ID.
        especificacao : str
            Subject/description of the process.
        nivel_acesso : str
            Access level: "0" = public, "1" = restricted, "2" = secret.
        assuntos : list of {"codigo": str, "descricao": str}
            Subject codes from SEI taxonomy.
        interessados : list of {"sigla": str, "nome": str}
            Interested parties.
        observacao : str, optional
            Internal observation.
        id_hipotese_legal : str, optional
            Legal hypothesis ID for restricted/secret documents.
        id_unidade : str, optional
            Override default unit ID.
        """
        sigla, servico = self._auth()
        log.debug("SEI: gerar_procedimento tipo=%s unit=%s nivel=%s", id_tipo_procedimento, self._unit(id_unidade), nivel_acesso)
        AssuntoFactory = self._get_type("Assunto")
        InteressadoFactory = self._get_type("Interessado")
        ProcedimentoFactory = self._get_type("Procedimento")

        assuntos_obj = [
            AssuntoFactory(
                CodigoEstruturado=a["codigo"],
                Descricao=a.get("descricao", ""),
            )
            for a in (assuntos or [])
        ]
        interessados_obj = [
            InteressadoFactory(
                Sigla=i["sigla"],
                Nome=i.get("nome", ""),
            )
            for i in (interessados or [])
        ]

        proc = ProcedimentoFactory(
            IdTipoProcedimento=id_tipo_procedimento,
            Especificacao=especificacao,
            Assuntos=assuntos_obj or None,
            Interessados=interessados_obj or None,
            Observacao=observacao,
            NivelAcesso=nivel_acesso,
            IdHipoteseLegal=id_hipotese_legal,
        )
        try:
            result = await self._svc().gerarProcedimento(
                SiglaSistema=sigla,
                IdentificacaoServico=servico,
                IdUnidade=self._unit(id_unidade),
                Procedimento=proc,
                Documentos=None,
                ProcedimentosRelacionados=None,
                UnidadesEnvio=None,
                SinManterAbertoUnidade="S",
                SinEnviarEmailNotificacao="N",
                DataRetornoProgramado=None,
                DiasRetornoProgramado=None,
                SinDiasUteisRetornoProgramado=None,
                IdMarcador=None,
                TextoMarcador=None,
                DataControlePrazo=None,
                DiasControlePrazo=None,
                SinDiasUteisControlePrazo=None,
            )
            return _serialize_result(result) or {}
        except ZeepFault as exc:
            raise SEIError(str(exc), fault_code=getattr(exc, "code", None)) from exc

    async def incluir_documento(
        self,
        protocolo_procedimento: str,
        id_serie: str,
        tipo: str = "G",
        descricao: Optional[str] = None,
        conteudo_html: Optional[str] = None,
        nivel_acesso: str = "0",
        id_unidade: Optional[str] = None,
    ) -> dict:
        """Add a generated document (Documento Gerado) to an existing process.

        Parameters
        ----------
        protocolo_procedimento : str
            Protocol number of the target process (e.g. "35014.000001/2020-31").
        id_serie : str
            Series ID from listarSeries (document type within SEI).
        tipo : str
            Document origin: "G" = generated (default), "R" = received.
        descricao : str, optional
            Short description shown in the process tree.
        conteudo_html : str, optional
            HTML content of the document.  Will be base64-encoded before sending.
        nivel_acesso : str
            Access level: "0" = public, "1" = restricted, "2" = secret.
        id_unidade : str, optional
            Override default unit ID.
        """
        sigla, servico = self._auth()
        log.debug("SEI: incluir_documento proc=%s serie=%s tipo=%s unit=%s", protocolo_procedimento, id_serie, tipo, self._unit(id_unidade))
        DocumentoFactory = self._get_type("Documento")

        conteudo_b64: Optional[str] = None
        if conteudo_html:
            conteudo_b64 = base64.b64encode(conteudo_html.encode("utf-8")).decode("ascii")

        doc = DocumentoFactory(
            Tipo=tipo,
            ProtocoloProcedimento=protocolo_procedimento,
            IdSerie=id_serie,
            Descricao=descricao,
            Conteudo=conteudo_b64,
            NivelAcesso=nivel_acesso,
        )
        try:
            result = await self._svc().incluirDocumento(
                SiglaSistema=sigla,
                IdentificacaoServico=servico,
                IdUnidade=self._unit(id_unidade),
                Documento=doc,
            )
            return _serialize_result(result) or {}
        except ZeepFault as exc:
            raise SEIError(str(exc), fault_code=getattr(exc, "code", None)) from exc

    async def enviar_processo(
        self,
        protocolo: str,
        unidades_destino: list[str],
        sin_manter_aberto: str = "N",
        sin_enviar_email: str = "N",
        id_unidade: Optional[str] = None,
    ) -> str:
        """Send a process to one or more destination units.

        Parameters
        ----------
        protocolo : str
            Protocol number of the process to send.
        unidades_destino : list[str]
            List of unit IDs to send the process to.
        sin_manter_aberto : str
            "S" to keep the process open in the origin unit; "N" to close it.
        sin_enviar_email : str
            "S" to send e-mail notification to destination unit; "N" otherwise.
        id_unidade : str, optional
            Override default unit ID (origin unit).
        """
        sigla, servico = self._auth()
        log.debug("SEI: enviar_processo protocolo=%s destinos=%r unit=%s", protocolo, unidades_destino, self._unit(id_unidade))
        ArrayOfIdUnidade = self._get_type("ArrayOfIdUnidade")
        unidades_obj = ArrayOfIdUnidade(unidades_destino)
        try:
            result = await self._svc().enviarProcesso(
                SiglaSistema=sigla,
                IdentificacaoServico=servico,
                IdUnidade=self._unit(id_unidade),
                ProtocoloProcedimento=protocolo,
                UnidadesDestino=unidades_obj,
                SinManterAbertoUnidade=sin_manter_aberto,
                SinRemoverAnotacao="N",
                SinEnviarEmailNotificacao=sin_enviar_email,
                DataRetornoProgramado=None,
                DiasRetornoProgramado=None,
                SinDiasUteisRetornoProgramado=None,
                SinReabrir="N",
            )
            serialized = _serialize_result(result)
            return str(serialized) if serialized is not None else "ok"
        except ZeepFault as exc:
            raise SEIError(str(exc), fault_code=getattr(exc, "code", None)) from exc
