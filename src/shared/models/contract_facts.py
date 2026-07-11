"""Pydantic models for contract classification — Facts and Inferences.

These models represent the output of the Classification Engine (Phase 1).
They are pure data models, not SQLAlchemy ORM models — they are not persisted
in PostgreSQL. They flow through the MCP tool pipeline as JSON-serializable dicts.

See: docs-local/prompts/SPEC-licitacoes-engine.md, Section 3.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ── Enums ──────────────────────────────────────────────────────────────────

OBJETO_TIPOS = ["bem", "servico", "obra", "locacao", "alienacao", "outro"]
SERVICO_NATUREZAS = [
    "comum", "intelectual", "continuado", "terceirizacao",
    "manutencao", "capacitacao", "limpeza", "vigilancia", "consultoria", "outro",
]
TIC_SUBTIPOS = [
    "software", "hardware", "saas", "nuvem", "desenvolvimento",
    "sustentacao", "consultoria_tic", "seguranca", "telecom", "datacenter", "ia", "outro",
]
AQUISICAO_NATUREZAS = [
    "compra", "locacao", "servico", "assinatura", "licenca", "outsourcing", "comodato", "outro",
]
SOLUCAO_DISPONIVEL = ["sim", "nao", "parcialmente", "nao_sei"]


# ── Sub-models ─────────────────────────────────────────────────────────────

class ObjetoFacts(BaseModel):
    """Object being procured."""
    tipo: str = Field(default="", description="Type of object: bem, servico, obra, locacao, alienacao, outro")
    descricao: str = Field(default="", min_length=0, max_length=5000)


class ServicoFacts(BaseModel):
    """Service-specific facts. Only meaningful when objeto.tipo == 'servico'."""
    natureza: Optional[str] = Field(default=None, description="Nature of the service")
    mao_obra_dedicada: bool = False


class TicFacts(BaseModel):
    """ICT-specific facts."""
    envolve: bool = False
    subtipo: Optional[str] = Field(default=None, description="ICT sub-type")


class AquisicaoFacts(BaseModel):
    """Acquisition nature facts."""
    natureza: Optional[str] = Field(default=None, description="Nature of acquisition: compra, locacao, servico, ...")


class ComplexidadeFacts(BaseModel):
    """Risk and complexity flags."""
    inovacao: bool = False
    elevado_risco: bool = False
    integracao: bool = False
    sigilo: bool = False
    lgpd: bool = False
    missao_critica: bool = False
    afeta_producao: bool = False
    migracao: bool = False
    operacao_24x7: bool = False
    legado: bool = False


class MercadoFacts(BaseModel):
    """Market availability facts."""
    solucao_disponivel: Optional[str] = Field(default=None, description="sim, nao, parcialmente, nao_sei")


class ValorFacts(BaseModel):
    """Estimated value facts."""
    estimado: Optional[float] = Field(default=None, ge=0)
    moeda: str = "BRL"
    fonte_recurso: Optional[str] = None


class ContextoFacts(BaseModel):
    """Administrative context."""
    esfera: str = "federal"
    orgao: Optional[str] = None
    regime_juridico: str = "Lei 14.133/2021"
    plano_contratacoes_anual: bool = False


# ── Inferences sub-models ──────────────────────────────────────────────────

class DominioInferences(BaseModel):
    """Domain classifications derived from facts."""
    tic: bool = False
    engenharia: bool = False
    saude: bool = False


class ExecucaoInferences(BaseModel):
    """Execution classifications."""
    continuado: bool = False
    comum: bool = False
    terceirizacao: bool = False


class TecnologiaInferences(BaseModel):
    """Technology classifications. Only meaningful when dominio.tic == True."""
    saas: bool = False
    nuvem: bool = False
    software: bool = False
    hardware: bool = False
    desenvolvimento: bool = False
    sustentacao: bool = False
    ia: bool = False


class ComposicaoInferences(BaseModel):
    """Composite classifications derived from combinations."""
    tic_continuado_com_lgpd: bool = False
    exige_etp_robusto: bool = False
    exige_analise_riscos: bool = False


# ── Top-level models ───────────────────────────────────────────────────────

class ContractFacts(BaseModel):
    """Objective facts about a procurement.

    NEVER contains decisions (modality, procedure, SRP, etc.).
    Those are output of the Decision Engine, not input from the user.

    See: SPEC-licitacoes-engine.md, Section 3.2.
    """

    id: str = Field(default_factory=lambda: f"facts_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    objeto: ObjetoFacts = Field(default_factory=lambda: ObjetoFacts())
    servico: Optional[ServicoFacts] = None
    tic: Optional[TicFacts] = None
    aquisicao: Optional[AquisicaoFacts] = None
    complexidade: ComplexidadeFacts = Field(default_factory=lambda: ComplexidadeFacts())
    mercado: Optional[MercadoFacts] = None
    valor: Optional[ValorFacts] = None
    contexto: ContextoFacts = Field(default_factory=lambda: ContextoFacts())

    @model_validator(mode="after")
    def validate_integrity(self) -> "ContractFacts":
        """Cross-field integrity rules."""
        # If tic.envolve is True, objeto.tipo must be servico or bem
        if self.tic and self.tic.envolve:
            if self.objeto.tipo not in ("servico", "bem"):
                raise ValueError(
                    f"tic.envolve=True requires objeto.tipo in ['servico', 'bem'], "
                    f"got '{self.objeto.tipo}'"
                )
        # If servico.natureza is set, objeto.tipo must be servico
        if self.servico and self.servico.natureza and self.objeto.tipo != "servico":
            raise ValueError(
                f"servico.natureza is set but objeto.tipo='{self.objeto.tipo}' "
                f"(expected 'servico')"
            )
        return self


class Inferences(BaseModel):
    """Classifications derived deterministically from ContractFacts.

    The user NEVER answers these directly — they are computed by the
    InferenceEngine from the objective facts.

    See: SPEC-licitacoes-engine.md, Section 3.3.
    """

    id: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)

    dominio: DominioInferences = Field(default_factory=lambda: DominioInferences())
    execucao: ExecucaoInferences = Field(default_factory=lambda: ExecucaoInferences())
    tecnologia: TecnologiaInferences = Field(default_factory=lambda: TecnologiaInferences())
    composicao: ComposicaoInferences = Field(default_factory=lambda: ComposicaoInferences())
