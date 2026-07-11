"""Inference Engine — deterministic facts → inferences.

Applies a set of declarative rules to derive classifications (inferences)
from objective facts (contract_facts). No AI/LLM involved — pure rule evaluation.

See: docs-local/prompts/SPEC-licitacoes-engine.md, Section 3.3.
"""

from __future__ import annotations

from typing import Any

from src.shared.models.contract_facts import (
    ContractFacts,
    Inferences,
    DominioInferences,
    ExecucaoInferences,
    TecnologiaInferences,
    ComposicaoInferences,
)


class InferenceEngine:
    """Deterministic rule engine: ContractFacts → Inferences.

    Usage::

        engine = InferenceEngine()
        facts = ContractFacts(objeto={"tipo": "servico"}, ...)
        inferences = engine.apply(facts)
        # inferences.dominio.tic == True (if facts.tic.envolve)
    """

    def apply(self, facts: ContractFacts) -> Inferences:
        """Apply all inference rules and return populated Inferences."""
        inferences = Inferences(id=facts.id)

        # ── Domain inferences ──────────────────────────────────────────
        inferences.dominio = DominioInferences(
            tic=self._is_tic(facts),
            engenharia=self._is_engenharia(facts),
            saude=self._is_saude(facts),
        )

        # ── Execution inferences ───────────────────────────────────────
        inferences.execucao = ExecucaoInferences(
            continuado=self._is_continuado(facts),
            comum=self._is_comum(facts),
            terceirizacao=self._is_terceirizacao(facts),
        )

        # ── Technology inferences ──────────────────────────────────────
        inferences.tecnologia = TecnologiaInferences(
            saas=self._is_saas(facts),
            nuvem=self._is_nuvem(facts),
            software=self._is_software(facts),
            hardware=self._is_hardware(facts),
            desenvolvimento=self._is_desenvolvimento(facts),
            sustentacao=self._is_sustentacao(facts),
            ia=self._is_ia(facts),
        )

        # ── Composite inferences ───────────────────────────────────────
        inferences.composicao = ComposicaoInferences(
            tic_continuado_com_lgpd=(
                inferences.dominio.tic
                and inferences.execucao.continuado
                and self._has_lgpd(facts)
            ),
            exige_etp_robusto=self._exige_etp_robusto(facts),
            exige_analise_riscos=(
                self._has_lgpd(facts)
                or inferences.dominio.tic
                or self._has_elevado_risco(facts)
            ),
        )

        return inferences

    # ── Domain rules ─────────────────────────────────────────────────────

    @staticmethod
    def _is_tic(facts: ContractFacts) -> bool:
        """Derive dominio.tic from facts.tic.envolve."""
        return bool(facts.tic and facts.tic.envolve)

    @staticmethod
    def _is_engenharia(facts: ContractFacts) -> bool:
        """Derive dominio.engenharia from objeto.tipo."""
        return facts.objeto.tipo == "obra"

    @staticmethod
    def _is_saude(facts: ContractFacts) -> bool:
        """Derive dominio.saude. Placeholder — extend with specific rules."""
        return False

    # ── Execution rules ──────────────────────────────────────────────────

    @staticmethod
    def _is_continuado(facts: ContractFacts) -> bool:
        """Derive continuado from servico.natureza."""
        if not facts.servico:
            return False
        return facts.servico.natureza == "continuado"

    @staticmethod
    def _is_comum(facts: ContractFacts) -> bool:
        """A service is 'comum' if it's not intelectual, terceirizacao, or consultoria."""
        if not facts.servico or not facts.servico.natureza:
            return False
        return facts.servico.natureza not in ("intelectual", "terceirizacao", "consultoria")

    @staticmethod
    def _is_terceirizacao(facts: ContractFacts) -> bool:
        """Derive terceirizacao from servico.natureza."""
        if not facts.servico:
            return False
        return facts.servico.natureza == "terceirizacao"

    # ── Technology rules ─────────────────────────────────────────────────

    @staticmethod
    def _is_saas(facts: ContractFacts) -> bool:
        if not facts.tic:
            return False
        return facts.tic.subtipo == "saas"

    @staticmethod
    def _is_nuvem(facts: ContractFacts) -> bool:
        if not facts.tic:
            return False
        # SaaS implies cloud, and explicit 'nuvem' subtype
        return facts.tic.subtipo in ("saas", "nuvem")

    @staticmethod
    def _is_software(facts: ContractFacts) -> bool:
        if not facts.tic:
            return False
        return facts.tic.subtipo == "software"

    @staticmethod
    def _is_hardware(facts: ContractFacts) -> bool:
        if not facts.tic:
            return False
        return facts.tic.subtipo == "hardware"

    @staticmethod
    def _is_desenvolvimento(facts: ContractFacts) -> bool:
        if not facts.tic:
            return False
        return facts.tic.subtipo == "desenvolvimento"

    @staticmethod
    def _is_sustentacao(facts: ContractFacts) -> bool:
        if not facts.tic:
            return False
        return facts.tic.subtipo == "sustentacao"

    @staticmethod
    def _is_ia(facts: ContractFacts) -> bool:
        if not facts.tic:
            return False
        return facts.tic.subtipo == "ia"

    # ── Composite rules ──────────────────────────────────────────────────

    @staticmethod
    def _has_lgpd(facts: ContractFacts) -> bool:
        return facts.complexidade.lgpd

    @staticmethod
    def _has_elevado_risco(facts: ContractFacts) -> bool:
        return facts.complexidade.elevado_risco

    @staticmethod
    def _exige_etp_robusto(facts: ContractFacts) -> bool:
        """ETP robusto exigido quando não há solução pronta no mercado."""
        if not facts.mercado:
            return False
        return facts.mercado.solucao_disponivel == "nao"
