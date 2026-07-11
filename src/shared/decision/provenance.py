"""Provenance Tracker — audit trail for obligation activation.

Generates provenance metadata for each activated obligation, recording
which rule triggered it, when, why, and which version of the norm was used.

See: docs-local/prompts/SPEC-licitacoes-engine.md, Section 4.5.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def create_provenance(
    obligation_id: str,
    rule_id: str = "",
    reason: str = "",
    norm_version: str = "",
    *,
    resolved_at: Optional[datetime] = None,
) -> dict:
    """Create a provenance block for an activated obligation.

    Args:
        obligation_id: The obligation being activated (e.g. ``obrig_alinhamento_pdtic``).
        rule_id: Identifier of the rule that triggered activation.
        reason: Human-readable reason (e.g. ``dominio.tic == true``).
        norm_version: Version/date of the norm used (e.g. ``IN SGD/ME nº 94/2022 — vigente em 2026-07-10``).
        resolved_at: Timestamp of resolution (defaults to now UTC).

    Returns:
        A dict suitable for inclusion in ``runtime_context.active_obligations[].provenance``.
    """
    return {
        "obligation_id": obligation_id,
        "resolved_by": rule_id,
        "resolved_at": (resolved_at or datetime.now(timezone.utc)).isoformat(),
        "reason": reason,
        "norm_version": norm_version,
    }


def create_provenance_batch(
    obligations: list[dict],
    rule_id: str = "",
    *,
    resolved_at: Optional[datetime] = None,
) -> list[dict]:
    """Create provenance blocks for multiple obligations activated by the same rule.

    Each obligation dict should have at least ``id`` and ``reason`` keys.
    """
    results: list[dict] = []
    for ob in obligations:
        results.append(
            create_provenance(
                obligation_id=ob.get("id", "unknown"),
                rule_id=rule_id,
                reason=ob.get("reason", ""),
                norm_version=ob.get("norm_version", ""),
                resolved_at=resolved_at,
            )
        )
    return results
