"""DAG Composer — topological sort of workflow steps.

Builds a Directed Acyclic Graph from capability steps, orders them via
Kahn's algorithm, and detects cycles with precise diagnostics.

See: docs-local/prompts/SPEC-licitacoes-engine.md, Section 5.1.
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class CycleDetectedError(Exception):
    """Raised when a cycle is detected in the step dependency graph."""

    def __init__(self, cycles: list[list[str]]):
        self.cycles = cycles
        cycle_strs = [" → ".join(c) for c in cycles]
        super().__init__(
            f"Cycle(s) detected in workflow DAG: {'; '.join(cycle_strs)}"
        )


class DAGComposer:
    """Composes a workflow DAG from capability steps.

    Usage::

        composer = DAGComposer()
        capabilities = [
            {"capability": {"id": "exige_df_demanda", "steps": [...]}},
            {"capability": {"id": "exige_alinhamento_pdtic", "steps": [...]}},
        ]
        dag = composer.compose(capabilities)
        # dag["steps"] is topologically sorted
        # dag["layers"] groups steps by depth
    """

    def compose(self, capabilities: list[dict]) -> dict:
        """Compose a workflow DAG from a list of capability dicts.

        Each capability dict should have the shape::

            {"capability": {"id": "...", "steps": [...]}}

        Args:
            capabilities: List of capability dicts from runtime_context.

        Returns:
            A ``workflow_dag`` dict with:
            - ``steps``: topologically sorted step objects
            - ``layers``: steps grouped by depth (0 = no dependencies)
            - ``step_count``: total number of steps
            - ``provided_by``: mapping of step_id → capability_id
        """
        # ── Collect all steps ──────────────────────────────────────────
        all_steps: dict[str, dict] = {}       # step_id → step dict
        provided_by: dict[str, str] = {}       # step_id → capability_id

        for cap_wrapper in capabilities:
            cap = cap_wrapper.get("capability", {})
            cap_id = cap.get("id", "unknown")
            for step in cap.get("steps", []):
                step_id = step.get("id", "")
                if not step_id:
                    continue
                if step_id in all_steps:
                    # Duplicate step id across capabilities — merge or warn
                    # Currently: first capability wins, others are skipped
                    continue
                all_steps[step_id] = dict(step)
                provided_by[step_id] = cap_id

        # ── Build graph ────────────────────────────────────────────────
        # in_degree[step_id] = number of unmet dependencies
        # adjacency[step_id] = list of steps that depend on step_id
        in_degree: dict[str, int] = {sid: 0 for sid in all_steps}
        adjacency: dict[str, list[str]] = {sid: [] for sid in all_steps}

        for step_id, step in all_steps.items():
            deps = step.get("depends_on", [])
            if isinstance(deps, str):
                deps = [deps]
            for dep_id in deps:
                if dep_id in all_steps:
                    in_degree[step_id] += 1
                    adjacency.setdefault(dep_id, []).append(step_id)

        # ── Kahn's algorithm (topological sort) ────────────────────────
        queue: deque[str] = deque(
            sid for sid, deg in in_degree.items() if deg == 0
        )
        sorted_steps: list[dict] = []
        layers: list[list[dict]] = []

        while queue:
            # Process one layer at a time
            layer: list[dict] = []
            layer_ids: list[str] = []
            for _ in range(len(queue)):
                sid = queue.popleft()
                step = all_steps[sid]
                # Attach metadata
                step["_provided_by"] = provided_by.get(sid, "unknown")
                layer.append(step)
                layer_ids.append(sid)

            layers.append(layer)
            sorted_steps.extend(layer)

            # Reduce in-degree of dependents
            for sid in layer_ids:
                for dependent in adjacency.get(sid, []):
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)

        # ── Detect cycles ──────────────────────────────────────────────
        if len(sorted_steps) != len(all_steps):
            remaining = set(all_steps.keys()) - {s["id"] for s in sorted_steps}
            cycles = self._find_cycles(
                {sid: adjacency.get(sid, []) for sid in remaining},
                remaining,
            )
            raise CycleDetectedError(cycles or [list(remaining)])

        return {
            "steps": sorted_steps,
            "layers": [{s["id"]: s for s in layer} for layer in layers],
            "step_count": len(sorted_steps),
            "layer_count": len(layers),
            "provided_by": provided_by,
        }

    def _find_cycles(
        self, graph: dict[str, list[str]], nodes: set[str]
    ) -> list[list[str]]:
        """Find cycles in a subgraph using DFS. Returns list of cycles found."""
        cycles: list[list[str]] = []
        visited: set[str] = set()
        stack: list[str] = []

        def dfs(node: str) -> None:
            if node in stack:
                # Found a cycle
                cycle_start = stack.index(node)
                cycles.append(list(stack[cycle_start:]) + [node])
                return
            if node in visited:
                return
            visited.add(node)
            stack.append(node)
            for neighbor in graph.get(node, []):
                if neighbor in nodes:
                    dfs(neighbor)
            stack.pop()

        for node in sorted(nodes):
            if node not in visited:
                dfs(node)

        return cycles[:5]  # Limit to 5 cycles to avoid overwhelming output
