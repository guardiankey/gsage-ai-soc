"""Unit tests for SimpleRuleEvaluator."""

from __future__ import annotations

import pytest

from src.shared.models.contract_facts import ContractFacts, Inferences
from src.shared.decision.inference_engine import InferenceEngine
from src.shared.decision.rule_evaluator import SimpleRuleEvaluator


@pytest.fixture
def evaluator() -> SimpleRuleEvaluator:
    return SimpleRuleEvaluator()


@pytest.fixture
def facts() -> ContractFacts:
    return ContractFacts(
        objeto={"tipo": "servico", "descricao": "Teste"},
        servico={"natureza": "continuado"},
        tic={"envolve": True, "subtipo": "sustentacao"},
        complexidade={"lgpd": True},
        mercado={"solucao_disponivel": "sim"},
        valor={"estimado": 480000.00},
    )


@pytest.fixture
def inferences(facts: ContractFacts) -> Inferences:
    return InferenceEngine().apply(facts)


class TestBasicOperators:
    """Tests for basic rule evaluation operators."""

    def test_equals_true(self, evaluator, facts, inferences):
        rule = {"all_of": [{"field": "dominio.tic", "operator": "equals", "value": True}]}
        assert evaluator.evaluate(rule, facts, inferences) is True

    def test_equals_false(self, evaluator, facts, inferences):
        rule = {"all_of": [{"field": "dominio.engenharia", "operator": "equals", "value": True}]}
        assert evaluator.evaluate(rule, facts, inferences) is False

    def test_not_equals(self, evaluator, facts, inferences):
        rule = {"all_of": [{"field": "dominio.engenharia", "operator": "not_equals", "value": True}]}
        assert evaluator.evaluate(rule, facts, inferences) is True

    def test_gt_value(self, evaluator, facts, inferences):
        rule = {"all_of": [{"field": "valor.estimado", "operator": "gt", "value": 100000}]}
        assert evaluator.evaluate(rule, facts, inferences) is True

    def test_gt_value_false(self, evaluator, facts, inferences):
        rule = {"all_of": [{"field": "valor.estimado", "operator": "gt", "value": 500000}]}
        assert evaluator.evaluate(rule, facts, inferences) is False

    def test_lt_value(self, evaluator, facts, inferences):
        rule = {"all_of": [{"field": "valor.estimado", "operator": "lt", "value": 500000}]}
        assert evaluator.evaluate(rule, facts, inferences) is True


class TestCompositeRules:
    """Tests for all_of / any_of combinations."""

    def test_all_of_both_true(self, evaluator, facts, inferences):
        rule = {
            "all_of": [
                {"field": "dominio.tic", "operator": "equals", "value": True},
                {"field": "complexidade.lgpd", "operator": "equals", "value": True},
            ]
        }
        assert evaluator.evaluate(rule, facts, inferences) is True

    def test_all_of_one_false(self, evaluator, facts, inferences):
        rule = {
            "all_of": [
                {"field": "dominio.tic", "operator": "equals", "value": True},
                {"field": "dominio.engenharia", "operator": "equals", "value": True},
            ]
        }
        assert evaluator.evaluate(rule, facts, inferences) is False

    def test_any_of_one_true(self, evaluator, facts, inferences):
        rule = {
            "any_of": [
                {"field": "dominio.engenharia", "operator": "equals", "value": True},
                {"field": "dominio.tic", "operator": "equals", "value": True},
            ]
        }
        assert evaluator.evaluate(rule, facts, inferences) is True

    def test_any_of_none_true(self, evaluator, facts, inferences):
        rule = {
            "any_of": [
                {"field": "dominio.engenharia", "operator": "equals", "value": True},
                {"field": "dominio.saude", "operator": "equals", "value": True},
            ]
        }
        assert evaluator.evaluate(rule, facts, inferences) is False

    def test_all_of_and_any_of(self, evaluator, facts, inferences):
        rule = {
            "all_of": [
                {"field": "dominio.tic", "operator": "equals", "value": True},
            ],
            "any_of": [
                {"field": "complexidade.lgpd", "operator": "equals", "value": True},
            ],
        }
        # all_of matches AND any_of matches → true
        assert evaluator.evaluate(rule, facts, inferences) is True


class TestEmptyRules:
    """Tests for empty or missing rule conditions."""

    def test_empty_rule(self, evaluator, facts, inferences):
        rule = {}
        assert evaluator.evaluate(rule, facts, inferences) is True

    def test_empty_all_of(self, evaluator, facts, inferences):
        rule = {"all_of": [], "any_of": []}
        assert evaluator.evaluate(rule, facts, inferences) is True
