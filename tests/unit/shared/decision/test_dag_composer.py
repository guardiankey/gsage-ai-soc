"""Unit tests for DAGComposer — topological sort and cycle detection."""

from __future__ import annotations

import pytest

from src.shared.decision.dag_composer import DAGComposer, CycleDetectedError


@pytest.fixture
def composer() -> DAGComposer:
    return DAGComposer()


class TestLinearDAG:
    """Tests for simple linear dependency chains."""

    def test_single_step(self, composer):
        caps = [
            {"capability": {"id": "c1", "steps": [
                {"id": "a", "name": "A", "type": "task", "depends_on": []},
            ]}},
        ]
        dag = composer.compose(caps)
        assert dag["step_count"] == 1
        assert dag["layer_count"] == 1

    def test_linear_chain(self, composer):
        caps = [
            {"capability": {"id": "c1", "steps": [
                {"id": "a", "name": "A", "type": "task", "depends_on": []},
                {"id": "b", "name": "B", "type": "task", "depends_on": ["a"]},
                {"id": "c", "name": "C", "type": "task", "depends_on": ["b"]},
            ]}},
        ]
        dag = composer.compose(caps)
        assert dag["step_count"] == 3
        assert dag["layer_count"] == 3
        assert [s["id"] for s in dag["steps"]] == ["a", "b", "c"]


class TestParallelDAG:
    """Tests for parallel branches and merges."""

    def test_two_parallel_branches(self, composer):
        caps = [
            {"capability": {"id": "c1", "steps": [
                {"id": "root", "name": "Root", "type": "task", "depends_on": []},
            ]}},
            {"capability": {"id": "c2", "steps": [
                {"id": "b1", "name": "B1", "type": "task", "depends_on": ["root"]},
            ]}},
            {"capability": {"id": "c3", "steps": [
                {"id": "b2", "name": "B2", "type": "task", "depends_on": ["root"]},
            ]}},
            {"capability": {"id": "c4", "steps": [
                {"id": "merge", "name": "Merge", "type": "task", "depends_on": ["b1", "b2"]},
            ]}},
        ]
        dag = composer.compose(caps)
        assert dag["step_count"] == 4
        # layer 0: root; layer 1: b1 and b2 (parallel); layer 2: merge
        layer_ids = [list(l.keys()) for l in dag["layers"]]
        assert "root" in layer_ids[0]
        assert "b1" in layer_ids[1] and "b2" in layer_ids[1]
        assert "merge" in layer_ids[2]

    def test_multi_capability_steps(self, composer):
        """Steps from multiple capabilities compose correctly."""
        caps = [
            {"capability": {"id": "exige_df", "steps": [
                {"id": "a", "name": "A", "type": "task", "depends_on": []},
            ]}},
            {"capability": {"id": "exige_tic", "steps": [
                {"id": "b", "name": "B", "type": "task", "depends_on": ["a"]},
            ]}},
            {"capability": {"id": "exige_lgpd", "steps": [
                {"id": "c", "name": "C", "type": "task", "depends_on": ["a"]},
            ]}},
        ]
        dag = composer.compose(caps)
        assert dag["step_count"] == 3
        # a in layer 0; b and c in layer 1
        steps_by_id = {s["id"]: s for s in dag["steps"]}
        assert steps_by_id["a"]["_provided_by"] == "exige_df"
        assert steps_by_id["b"]["_provided_by"] == "exige_tic"
        assert steps_by_id["c"]["_provided_by"] == "exige_lgpd"


class TestCycleDetection:
    """Tests for cycle detection in dependency graphs."""

    def test_simple_cycle(self, composer):
        caps = [
            {"capability": {"id": "c1", "steps": [
                {"id": "a", "name": "A", "type": "task", "depends_on": ["b"]},
                {"id": "b", "name": "B", "type": "task", "depends_on": ["a"]},
            ]}},
        ]
        with pytest.raises(CycleDetectedError) as exc:
            composer.compose(caps)
        assert "a" in str(exc.value) and "b" in str(exc.value)

    def test_three_node_cycle(self, composer):
        caps = [
            {"capability": {"id": "c1", "steps": [
                {"id": "a", "name": "A", "type": "task", "depends_on": ["b"]},
                {"id": "b", "name": "B", "type": "task", "depends_on": ["c"]},
                {"id": "c", "name": "C", "type": "task", "depends_on": ["a"]},
            ]}},
        ]
        with pytest.raises(CycleDetectedError):
            composer.compose(caps)

    def test_no_cycle_valid_dag(self, composer):
        caps = [
            {"capability": {"id": "c1", "steps": [
                {"id": "a", "name": "A", "type": "task", "depends_on": []},
                {"id": "b", "name": "B", "type": "task", "depends_on": ["a"]},
                {"id": "c", "name": "C", "type": "task", "depends_on": ["a"]},
                {"id": "d", "name": "D", "type": "task", "depends_on": ["b", "c"]},
            ]}},
        ]
        dag = composer.compose(caps)
        assert dag["step_count"] == 4


class TestProvidedBy:
    """Tests for step → capability traceability."""

    def test_provided_by_metadata(self, composer):
        caps = [
            {"capability": {"id": "exige_df", "steps": [
                {"id": "x", "name": "X", "type": "task", "depends_on": []},
            ]}},
            {"capability": {"id": "exige_tic", "steps": [
                {"id": "y", "name": "Y", "type": "task", "depends_on": ["x"]},
            ]}},
        ]
        dag = composer.compose(caps)
        steps_by_id = {s["id"]: s for s in dag["steps"]}
        assert steps_by_id["x"]["_provided_by"] == "exige_df"
        assert steps_by_id["y"]["_provided_by"] == "exige_tic"
