"""Unit tests for ProvenanceTracker."""

from __future__ import annotations

from src.shared.decision.provenance import create_provenance, create_provenance_batch


class TestCreateProvenance:
    """Tests for single provenance creation."""

    def test_basic_provenance(self):
        prov = create_provenance(
            obligation_id="obrig_alinhamento_pdtic",
            rule_id="rule.tic.alinhamento",
            reason="dominio.tic == true",
            norm_version="IN SGD/ME nº 94/2022 — vigente em 2026-07-10",
        )
        assert prov["obligation_id"] == "obrig_alinhamento_pdtic"
        assert prov["resolved_by"] == "rule.tic.alinhamento"
        assert prov["reason"] == "dominio.tic == true"
        assert prov["norm_version"] == "IN SGD/ME nº 94/2022 — vigente em 2026-07-10"
        assert "resolved_at" in prov

    def test_provenance_with_defaults(self):
        prov = create_provenance(obligation_id="test_ob")
        assert prov["obligation_id"] == "test_ob"
        assert prov["resolved_by"] == ""
        assert prov["reason"] == ""
        assert prov["norm_version"] == ""
        assert "resolved_at" in prov


class TestCreateProvenanceBatch:
    """Tests for batch provenance creation."""

    def test_batch(self):
        obligations = [
            {"id": "ob_a", "reason": "dominio.tic == true", "norm_version": "IN 94/2022"},
            {"id": "ob_b", "reason": "complexidade.lgpd == true", "norm_version": "LGPD"},
        ]
        results = create_provenance_batch(obligations, rule_id="rule.test")
        assert len(results) == 2
        assert results[0]["obligation_id"] == "ob_a"
        assert results[0]["resolved_by"] == "rule.test"
        assert results[1]["obligation_id"] == "ob_b"
        assert results[1]["resolved_by"] == "rule.test"

    def test_batch_with_missing_fields(self):
        obligations = [{"id": "ob_x"}]
        results = create_provenance_batch(obligations, rule_id="rule.x")
        assert len(results) == 1
        assert results[0]["obligation_id"] == "ob_x"
        assert results[0]["reason"] == ""
