"""Rule Evaluator — pluggable rule evaluation engine.

Evaluates activation rules (``all_of``, ``any_of``) against contract facts
and inferences. Designed with a Protocol interface so the rule engine can
be swapped (e.g., DMN, Drools, json-rules-engine) without changing the
rest of the system.

See: docs-local/prompts/SPEC-licitacoes-engine.md, Section 4.5 and 10.5.
"""

from __future__ import annotations

from typing import Any, Protocol

from src.shared.models.contract_facts import ContractFacts, Inferences


# ── Protocol ───────────────────────────────────────────────────────────────

class RuleEvaluator(Protocol):
    """Protocol for pluggable rule evaluation engines."""

    def evaluate(self, rule: dict, facts: ContractFacts, inferences: Inferences) -> bool:
        """Evaluate a rule dict against facts + inferences.

        Args:
            rule: A dict with ``all_of`` and/or ``any_of`` lists of conditions.
            facts: Objective contract facts.
            inferences: Derived inferences.

        Returns:
            True if the rule matches.
        """
        ...


# ── Simple implementation ──────────────────────────────────────────────────

class SimpleRuleEvaluator:
    """YAML-based rule evaluator.

    Supports conditions with operators: ``equals``, ``not_equals``, ``in``, ``gt``, ``lt``.

    Rule format::

        {
            "all_of": [
                {"field": "dominio.tic", "operator": "equals", "value": true},
                {"field": "valor.estimado", "operator": "gt", "value": 100000},
            ],
            "any_of": []
        }

    Field paths use dot notation: ``dominio.tic``, ``complexidade.lgpd``,
    ``valor.estimado``, ``objeto.tipo``.
    """

    # Operators that require no external dependencies.
    _OPERATORS = {
        "equals": lambda a, b: a == b,
        "not_equals": lambda a, b: a != b,
        "gt": lambda a, b: a is not None and b is not None and a > b,
        "lt": lambda a, b: a is not None and b is not None and a < b,
        "gte": lambda a, b: a is not None and b is not None and a >= b,
        "lte": lambda a, b: a is not None and b is not None and a <= b,
        "in": lambda a, b: a in b if b is not None else False,
    }

    def evaluate(self, rule: dict, facts: ContractFacts, inferences: Inferences) -> bool:
        """Evaluate a rule against facts + inferences.

        Returns True if ALL ``all_of`` conditions match AND at least one
        ``any_of`` condition matches (if any_of is non-empty).
        """
        all_of = rule.get("all_of") or []
        any_of = rule.get("any_of") or []

        # All all_of conditions must match.
        for condition in all_of:
            if not self._evaluate_condition(condition, facts, inferences):
                return False

        # If any_of is specified, at least one must match.
        if any_of:
            if not any(
                self._evaluate_condition(c, facts, inferences) for c in any_of
            ):
                return False

        return True

    def _evaluate_condition(
        self, condition: dict, facts: ContractFacts, inferences: Inferences
    ) -> bool:
        """Evaluate a single condition."""
        field_path: str = condition.get("field", "")
        operator: str = condition.get("operator", "equals")
        expected_value = condition.get("value")

        actual_value = self._resolve_field(field_path, facts, inferences)
        op_func = self._OPERATORS.get(operator)
        if op_func is None:
            raise ValueError(f"Unknown operator: {operator}")
        return op_func(actual_value, expected_value)

    def _resolve_field(
        self, field_path: str, facts: ContractFacts, inferences: Inferences
    ) -> Any:
        """Resolve a dot-notation field path against facts + inferences.

        Paths starting with ``dominio.``, ``execucao.``, ``tecnologia.``,
        ``composicao.`` are resolved against inferences. All others are
        resolved against facts.

        Examples:
            ``dominio.tic`` → inferences.dominio.tic
            ``objeto.tipo`` → facts.objeto.tipo
            ``complexidade.lgpd`` → facts.complexidade.lgpd
            ``valor.estimado`` → facts.valor.estimado
        """
        parts = field_path.split(".")
        root = parts[0]

        # Inference fields
        if root in ("dominio", "execucao", "tecnologia", "composicao"):
            target: Any = inferences
        else:
            target = facts

        # Traverse the path
        for part in parts:
            if target is None:
                return None
            if isinstance(target, dict):
                target = target.get(part)
            else:
                target = getattr(target, part, None)
        return target
