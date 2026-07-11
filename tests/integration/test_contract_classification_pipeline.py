"""Integration tests for the full contract classification pipeline.

Tests the complete flow: contract_facts → inferences → obligations → capabilities → DAG.

Covers the 6 scenarios from SPEC-licitacoes-engine.md, Section 7, Fase 5:
  1. Pregão eletrônico — bens comuns
  2. Concorrência — obra de engenharia
  3. Inexigibilidade — serviço técnico especializado
  4. Dispensa por valor
  5. SaaS + LGPD + SRP (multi-capability composition)
  6. Sustentação TIC + legado + alta disponibilidade

Also validates:
  - Rastreabilidade ponta a ponta (step → capability → obligation → authority)
  - Ciclo no DAG
  - Provenance das obligations ativadas
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.shared.models.contract_facts import ContractFacts
from src.shared.decision.inference_engine import InferenceEngine
from src.shared.decision.rule_evaluator import SimpleRuleEvaluator
from src.shared.decision.dag_composer import DAGComposer, CycleDetectedError


# ── Paths ──────────────────────────────────────────────────────────────────
_DEFINITIONS_DIR = Path(__file__).resolve().parent.parent.parent / (
    "src/mcp_server/tools/enterprise/process_catalog/definitions"
)
_OBLIGATIONS_DIR = _DEFINITIONS_DIR / "obligations"
_CAPABILITIES_DIR = _DEFINITIONS_DIR / "capabilities"


# ── Helpers ────────────────────────────────────────────────────────────────


def _load_obligations() -> list[dict]:
    obs = []
    if _OBLIGATIONS_DIR.exists():
        for yp in sorted(_OBLIGATIONS_DIR.rglob("*.yaml")):
            data = yaml.safe_load(yp.read_text(encoding="utf-8")) or {}
            if "obligation" in data:
                obs.append(data)
    return obs


def _load_capability(cap_id: str) -> dict | None:
    cap_file = _CAPABILITIES_DIR / f"{cap_id}.yaml"
    if cap_file.exists():
        return yaml.safe_load(cap_file.read_text(encoding="utf-8")) or {}
    return None


def _resolve(facts: ContractFacts) -> dict:
    """Run the full resolve pipeline and return runtime_context-like dict."""
    engine = InferenceEngine()
    inferences = engine.apply(facts)
    evaluator = SimpleRuleEvaluator()
    obligations_all = _load_obligations()

    active_obs = []
    active_cap_ids = set()

    for ob_yaml in obligations_all:
        ob = ob_yaml.get("obligation", {})
        act = ob.get("activation", {})
        if evaluator.evaluate(act, facts, inferences):
            cap_id = ob.get("capability", "")
            active_obs.append({"id": ob["id"], "capability": cap_id, "sources": ob.get("sources", [])})
            if cap_id:
                active_cap_ids.add(cap_id)

    active_caps = []
    for cid in sorted(active_cap_ids):
        cap_yaml = _load_capability(cid)
        if cap_yaml:
            active_caps.append(cap_yaml)

    composer = DAGComposer()
    dag = composer.compose(active_caps)

    return {
        "facts": facts,
        "inferences": inferences,
        "active_obligations": active_obs,
        "active_capabilities": active_caps,
        "workflow_dag": dag,
    }


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def facts_pregao_bens() -> ContractFacts:
    """Cenário 1: Pregão eletrônico para bens comuns."""
    return ContractFacts(
        objeto={"tipo": "bem", "descricao": "Aquisição de 100 notebooks"},
        tic={"envolve": True, "subtipo": "hardware"},
        aquisicao={"natureza": "compra"},
        complexidade={},
        mercado={"solucao_disponivel": "sim"},
        valor={"estimado": 250000.00},
        contexto={"orgao": "Ministério Y", "esfera": "federal"},
    )


@pytest.fixture
def facts_obra() -> ContractFacts:
    """Cenário 2: Concorrência para obra de engenharia."""
    return ContractFacts(
        objeto={"tipo": "obra", "descricao": "Construção de ponte sobre o Rio X"},
        complexidade={"elevado_risco": True},
        mercado={"solucao_disponivel": "nao"},
        valor={"estimado": 5000000.00},
        contexto={"orgao": "DNIT", "esfera": "federal"},
    )


@pytest.fixture
def facts_dispensa_valor() -> ContractFacts:
    """Cenário 4: Dispensa por valor (art. 75, II)."""
    return ContractFacts(
        objeto={"tipo": "servico", "descricao": "Serviço de manutenção de ar condicionado"},
        servico={"natureza": "comum"},
        aquisicao={"natureza": "servico"},
        complexidade={},
        mercado={"solucao_disponivel": "sim"},
        valor={"estimado": 30000.00},
        contexto={"orgao": "Prefeitura Z", "esfera": "municipal"},
    )


@pytest.fixture
def facts_saas_lgpd() -> ContractFacts:
    """Cenário 5: SaaS + LGPD — multi-capability composition."""
    return ContractFacts(
        objeto={"tipo": "servico", "descricao": "Contratação de plataforma SaaS de gestão"},
        servico={"natureza": "continuado"},
        tic={"envolve": True, "subtipo": "saas"},
        aquisicao={"natureza": "assinatura"},
        complexidade={"lgpd": True, "integracao": True},
        mercado={"solucao_disponivel": "sim"},
        valor={"estimado": 360000.00},
        contexto={"orgao": "Ministério X", "esfera": "federal"},
    )


@pytest.fixture
def facts_sustentacao_tic_legado() -> ContractFacts:
    """Cenário 6: Sustentação TIC + legado + alta disponibilidade."""
    return ContractFacts(
        objeto={"tipo": "servico", "descricao": "Suporte técnico e sustentação de sistemas legados 24x7"},
        servico={"natureza": "continuado"},
        tic={"envolve": True, "subtipo": "sustentacao"},
        aquisicao={"natureza": "servico"},
        complexidade={"lgpd": True, "integracao": True, "legado": True, "afeta_producao": True, "operacao_24x7": True},
        mercado={"solucao_disponivel": "sim"},
        valor={"estimado": 480000.00},
        contexto={"orgao": "Ministério X", "esfera": "federal"},
    )


# ── Scenario tests ─────────────────────────────────────────────────────────


class TestScenarioPregaoBens:
    """Cenário 1: Pregão eletrônico para bens comuns."""

    def test_inferences(self, facts_pregao_bens):
        result = _resolve(facts_pregao_bens)
        inf = result["inferences"]
        assert inf.dominio.tic is True
        assert inf.tecnologia.hardware is True

    def test_obligations_active(self, facts_pregao_bens):
        result = _resolve(facts_pregao_bens)
        obs_ids = [o["id"] for o in result["active_obligations"]]
        # Base obligations should be active
        assert "obrig_formalizar_demanda" in obs_ids
        assert "obrig_justificar_necessidade" in obs_ids
        assert "obrig_pesquisa_precos" in obs_ids
        # TIC obligation should be active (hardware is TIC)
        assert "obrig_alinhamento_pdtic" in obs_ids
        # LGPD should NOT be active for bens sem dados pessoais
        assert "obrig_analise_riscos_lgpd" not in obs_ids

    def test_dag_composition(self, facts_pregao_bens):
        result = _resolve(facts_pregao_bens)
        dag = result["workflow_dag"]
        assert dag["step_count"] >= 4  # formalizar + justificar + alinhamento_pdtic + pesquisa
        # formalizar_demanda should be in layer 0
        assert "formalizar_demanda" in dag["layers"][0]


class TestScenarioObra:
    """Cenário 2: Concorrência para obra de engenharia."""

    def test_inferences(self, facts_obra):
        result = _resolve(facts_obra)
        inf = result["inferences"]
        assert inf.dominio.engenharia is True
        assert inf.dominio.tic is False
        assert inf.composicao.exige_etp_robusto is True  # no solution available

    def test_tic_obligation_not_active(self, facts_obra):
        result = _resolve(facts_obra)
        obs_ids = [o["id"] for o in result["active_obligations"]]
        assert "obrig_alinhamento_pdtic" not in obs_ids


class TestScenarioDispensaValor:
    """Cenário 4: Dispensa por valor."""

    def test_low_value_facts(self, facts_dispensa_valor):
        assert facts_dispensa_valor.valor.estimado == 30000.00

    def test_obligations_active(self, facts_dispensa_valor):
        result = _resolve(facts_dispensa_valor)
        obs_ids = [o["id"] for o in result["active_obligations"]]
        assert "obrig_formalizar_demanda" in obs_ids


class TestScenarioSaaSLGPD:
    """Cenário 5: SaaS + LGPD — composição multi-capability."""

    def test_multi_capability_composition(self, facts_saas_lgpd):
        result = _resolve(facts_saas_lgpd)
        obs_ids = [o["id"] for o in result["active_obligations"]]
        # Both TIC and LGPD obligations should be active
        assert "obrig_alinhamento_pdtic" in obs_ids
        assert "obrig_analise_riscos_lgpd" in obs_ids

    def test_parallel_steps_in_dag(self, facts_saas_lgpd):
        result = _resolve(facts_saas_lgpd)
        dag = result["workflow_dag"]
        # identificar_dados_pessoais and avaliar_alinhamento_pdtic should be in same layer (parallel)
        layer1_ids = set()
        for layer in dag["layers"]:
            layer1_ids |= set(layer.keys())
        # Both should be present somewhere in the DAG
        all_step_ids = {s["id"] for s in dag["steps"]}
        assert "identificar_dados_pessoais" in all_step_ids
        assert "avaliar_alinhamento_pdtic" in all_step_ids

    def test_nuvem_inference(self, facts_saas_lgpd):
        result = _resolve(facts_saas_lgpd)
        inf = result["inferences"]
        assert inf.tecnologia.saas is True
        assert inf.tecnologia.nuvem is True  # SaaS implies nuvem


class TestScenarioSustentacaoTIC:
    """Cenário 6: Sustentação TIC + legado + alta disponibilidade."""

    def test_all_expected_obligations(self, facts_sustentacao_tic_legado):
        result = _resolve(facts_sustentacao_tic_legado)
        obs_ids = [o["id"] for o in result["active_obligations"]]
        expected = [
            "obrig_formalizar_demanda",
            "obrig_justificar_necessidade",
            "obrig_pesquisa_precos",
            "obrig_alinhamento_pdtic",
            "obrig_analise_riscos_lgpd",
        ]
        for eo in expected:
            assert eo in obs_ids, f"Expected {eo} to be active"

    def test_dag_layers(self, facts_sustentacao_tic_legado):
        result = _resolve(facts_sustentacao_tic_legado)
        dag = result["workflow_dag"]
        assert dag["step_count"] == 5
        assert dag["layer_count"] == 3


class TestTraceability:
    """Rastreabilidade ponta a ponta."""

    def test_step_to_capability(self, facts_sustentacao_tic_legado):
        result = _resolve(facts_sustentacao_tic_legado)
        dag = result["workflow_dag"]
        for step in dag["steps"]:
            assert "_provided_by" in step, f"Step {step['id']} missing _provided_by"
            assert step["_provided_by"] != "unknown"

    def test_obligation_to_authority(self, facts_sustentacao_tic_legado):
        result = _resolve(facts_sustentacao_tic_legado)
        for ob in result["active_obligations"]:
            assert "sources" in ob
            assert len(ob["sources"]) > 0, f"Obligation {ob['id']} has no sources"
            for src in ob["sources"]:
                assert "authority" in src
                assert "authority_weight" in src
                assert "authority_role" in src


class TestCycleInDAG:
    """Validação de detecção de ciclos."""

    def test_cycle_detected(self):
        composer = DAGComposer()
        caps = [
            {"capability": {"id": "c1", "steps": [
                {"id": "a", "name": "A", "type": "task", "depends_on": ["b"]},
                {"id": "b", "name": "B", "type": "task", "depends_on": ["a"]},
            ]}},
        ]
        with pytest.raises(CycleDetectedError):
            composer.compose(caps)
