"""Unit tests for ContractFacts and Inferences Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.shared.models.contract_facts import (
    AquisicaoFacts,
    ComplexidadeFacts,
    ContextoFacts,
    ContractFacts,
    Inferences,
    MercadoFacts,
    ObjetoFacts,
    ServicoFacts,
    TicFacts,
    ValorFacts,
)


class TestContractFactsValidation:
    """Tests for ContractFacts model validation."""

    def test_minimal_facts(self):
        facts = ContractFacts(objeto=ObjetoFacts(tipo="servico", descricao="Teste"))
        assert facts.objeto.tipo == "servico"
        assert facts.id.startswith("facts_")

    def test_defaults(self):
        facts = ContractFacts(objeto=ObjetoFacts(tipo="bem", descricao="Teste"))
        assert facts.complexidade.lgpd is False
        assert facts.complexidade.inovacao is False
        assert facts.contexto.esfera == "federal"
        assert facts.contexto.regime_juridico == "Lei 14.133/2021"

    def test_tic_with_servico_valid(self):
        facts = ContractFacts(
            objeto=ObjetoFacts(tipo="servico", descricao="Suporte"),
            tic=TicFacts(envolve=True, subtipo="sustentacao"),
            complexidade=ComplexidadeFacts(),
        )
        assert facts.tic is not None
        assert facts.tic.envolve is True
        assert facts.tic.subtipo == "sustentacao"

    def test_tic_with_obra_invalid(self):
        """tic.envolve=True requires objeto.tipo in [servico, bem]."""
        with pytest.raises(ValidationError):
            ContractFacts(
                objeto=ObjetoFacts(tipo="obra", descricao="Ponte"),
                tic=TicFacts(envolve=True, subtipo="hardware"),
                complexidade=ComplexidadeFacts(),
            )

    def test_servico_natureza_without_servico_invalid(self):
        """servico.natureza requires objeto.tipo == 'servico'."""
        with pytest.raises(ValidationError):
            ContractFacts(
                objeto=ObjetoFacts(tipo="bem", descricao="Notebook"),
                servico=ServicoFacts(natureza="continuado"),
                complexidade=ComplexidadeFacts(),
            )

    def test_full_facts(self):
        facts = ContractFacts(
            objeto=ObjetoFacts(tipo="servico", descricao="Suporte técnico"),
            servico=ServicoFacts(natureza="continuado"),
            tic=TicFacts(envolve=True, subtipo="sustentacao"),
            aquisicao=AquisicaoFacts(natureza="servico"),
            complexidade=ComplexidadeFacts(lgpd=True, integracao=True, legado=True),
            mercado=MercadoFacts(solucao_disponivel="sim"),
            valor=ValorFacts(estimado=480000.00, moeda="BRL"),
            contexto=ContextoFacts(orgao="Ministério X", esfera="federal"),
        )
        assert facts.objeto.tipo == "servico"
        assert facts.servico is not None
        assert facts.servico.natureza == "continuado"
        assert facts.valor is not None
        assert facts.valor.estimado == 480000.00

    def test_serialization(self):
        facts = ContractFacts(
            objeto=ObjetoFacts(tipo="bem", descricao="Notebooks"),
            complexidade=ComplexidadeFacts(),
            valor=ValorFacts(estimado=100000.00),
        )
        data = facts.model_dump(mode="json")
        assert data["objeto"]["tipo"] == "bem"
        assert data["valor"]["estimado"] == 100000.00
        assert "id" in data
        assert "created_at" in data


class TestInferences:
    """Tests for Inferences model."""

    def test_default_inferences(self):
        inf = Inferences()
        assert inf.dominio.tic is False
        assert inf.execucao.continuado is False
        assert inf.composicao.exige_etp_robusto is False

    def test_serialization(self):
        inf = Inferences(id="test_001")
        inf.dominio.tic = True
        inf.execucao.continuado = True
        data = inf.model_dump(mode="json")
        assert data["dominio"]["tic"] is True
        assert data["execucao"]["continuado"] is True


class TestObjetoFacts:
    """Tests for ObjetoFacts sub-model."""

    def test_default_tipo(self):
        obj = ObjetoFacts()
        assert obj.tipo == ""

    def test_with_tipo(self):
        obj = ObjetoFacts(tipo="servico", descricao="Teste")
        assert obj.tipo == "servico"


class TestComplexidadeFacts:
    """Tests for ComplexidadeFacts sub-model."""

    def test_all_default_false(self):
        c = ComplexidadeFacts()
        assert c.lgpd is False
        assert c.inovacao is False
        assert c.sigilo is False
