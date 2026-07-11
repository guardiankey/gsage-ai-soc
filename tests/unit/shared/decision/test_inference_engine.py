"""Unit tests for InferenceEngine — deterministic facts → inferences."""

from __future__ import annotations

import pytest

from src.shared.models.contract_facts import ContractFacts, Inferences
from src.shared.decision.inference_engine import InferenceEngine


@pytest.fixture
def engine() -> InferenceEngine:
    return InferenceEngine()


@pytest.fixture
def facts_tic_continuado_lgpd() -> ContractFacts:
    """Sustentação de TIC, serviço continuado, com LGPD."""
    return ContractFacts(
        objeto={"tipo": "servico", "descricao": "Suporte técnico e sustentação de sistemas legados"},
        servico={"natureza": "continuado"},
        tic={"envolve": True, "subtipo": "sustentacao"},
        aquisicao={"natureza": "servico"},
        complexidade={"lgpd": True, "integracao": True, "legado": True},
        mercado={"solucao_disponivel": "sim"},
        valor={"estimado": 480000.00},
        contexto={"orgao": "Ministério X"},
    )


@pytest.fixture
def facts_bens_comuns() -> ContractFacts:
    """Pregão para bens comuns."""
    return ContractFacts(
        objeto={"tipo": "bem", "descricao": "Aquisição de notebooks"},
        tic={"envolve": True, "subtipo": "hardware"},
        complexidade={},
        mercado={"solucao_disponivel": "sim"},
        valor={"estimado": 120000.00},
        contexto={"orgao": "Ministério Y"},
    )


@pytest.fixture
def facts_obra() -> ContractFacts:
    """Obra de engenharia."""
    return ContractFacts(
        objeto={"tipo": "obra", "descricao": "Construção de ponte"},
        complexidade={"elevado_risco": True},
        mercado={"solucao_disponivel": "nao"},
        valor={"estimado": 5000000.00},
        contexto={"orgao": "DNIT"},
    )


class TestDomainInferences:
    """Tests for dominio.* inferences."""

    def test_tic_true_when_tic_envolve(self, engine, facts_tic_continuado_lgpd):
        inferences = engine.apply(facts_tic_continuado_lgpd)
        assert inferences.dominio.tic is True

    def test_tic_false_when_no_tic(self, engine, facts_obra):
        inferences = engine.apply(facts_obra)
        assert inferences.dominio.tic is False

    def test_engenharia_true_for_obra(self, engine, facts_obra):
        inferences = engine.apply(facts_obra)
        assert inferences.dominio.engenharia is True

    def test_engenharia_false_for_servico(self, engine, facts_tic_continuado_lgpd):
        inferences = engine.apply(facts_tic_continuado_lgpd)
        assert inferences.dominio.engenharia is False


class TestExecutionInferences:
    """Tests for execucao.* inferences."""

    def test_continuado_true(self, engine, facts_tic_continuado_lgpd):
        inferences = engine.apply(facts_tic_continuado_lgpd)
        assert inferences.execucao.continuado is True

    def test_continuado_false_when_no_servico(self, engine, facts_bens_comuns):
        inferences = engine.apply(facts_bens_comuns)
        assert inferences.execucao.continuado is False

    def test_comum_true_for_continuado(self, engine, facts_tic_continuado_lgpd):
        inferences = engine.apply(facts_tic_continuado_lgpd)
        assert inferences.execucao.comum is True


class TestTechnologyInferences:
    """Tests for tecnologia.* inferences."""

    def test_sustentacao(self, engine, facts_tic_continuado_lgpd):
        inferences = engine.apply(facts_tic_continuado_lgpd)
        assert inferences.tecnologia.sustentacao is True

    def test_hardware(self, engine, facts_bens_comuns):
        inferences = engine.apply(facts_bens_comuns)
        assert inferences.tecnologia.hardware is True

    def test_nuvem_implied_by_saas(self, engine):
        facts = ContractFacts(
            objeto={"tipo": "servico"},
            tic={"envolve": True, "subtipo": "saas"},
            complexidade={},
            mercado={"solucao_disponivel": "sim"},
        )
        inferences = engine.apply(facts)
        assert inferences.tecnologia.nuvem is True  # SaaS implies cloud


class TestCompositeInferences:
    """Tests for composicao.* inferences."""

    def test_tic_continuado_com_lgpd(self, engine, facts_tic_continuado_lgpd):
        inferences = engine.apply(facts_tic_continuado_lgpd)
        assert inferences.composicao.tic_continuado_com_lgpd is True

    def test_exige_analise_riscos_with_lgpd(self, engine, facts_tic_continuado_lgpd):
        inferences = engine.apply(facts_tic_continuado_lgpd)
        assert inferences.composicao.exige_analise_riscos is True

    def test_exige_etp_robusto_when_no_solution(self, engine, facts_obra):
        inferences = engine.apply(facts_obra)
        assert inferences.composicao.exige_etp_robusto is True

    def test_exige_etp_robusto_false_when_solution_exists(self, engine, facts_tic_continuado_lgpd):
        inferences = engine.apply(facts_tic_continuado_lgpd)
        assert inferences.composicao.exige_etp_robusto is False


class TestInferencesSerialization:
    """Tests for JSON serialization."""

    def test_model_dump(self, engine, facts_tic_continuado_lgpd):
        inferences = engine.apply(facts_tic_continuado_lgpd)
        data = inferences.model_dump(mode="json")
        assert data["dominio"]["tic"] is True
        assert data["execucao"]["continuado"] is True
        assert "created_at" in data
