"""Live integration tests for Trellix EDR alerts & threats tools.

Requires valid Trellix credentials in environment variables:
    TRELLIX_CLIENT_ID
    TRELLIX_CLIENT_SECRET
    TRELLIX_X_API_KEY

Usage:
    source limbo/trellix.sh
    pytest tests/integration/test_trellix_edr_alerts_threats.py -v -m trellix_live

Skip in CI:
    pytest -m "not trellix_live"
"""

from __future__ import annotations

import os
import uuid

import pytest

from src.mcp_server.tools.soc.edr.trellix.trellix_edr_alerts import TrellixEdrAlertsTool
from src.mcp_server.tools.soc.edr.trellix.trellix_edr_threats import TrellixEdrThreatsTool
from src.shared.security.context import AgentContext, RequestSource


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def agent_context() -> AgentContext:
    """Minimal agent context for tool execution (org/user IDs are placeholders)."""
    org_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    return AgentContext(
        org_id=org_id,
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        group_ids=[uuid.UUID("00000000-0000-0000-0000-000000000010")],
        permissions=["edr:read"],
        request_id=uuid.uuid4(),
        source=RequestSource.API,
    )


@pytest.fixture
def trellix_config() -> dict:
    """Read Trellix credentials from environment (same source as limbo/trellix.sh)."""
    missing = []
    for var in ("TRELLIX_CLIENT_ID", "TRELLIX_CLIENT_SECRET", "TRELLIX_X_API_KEY"):
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        pytest.fail(f"Missing required env vars: {', '.join(missing)}.  Run: source limbo/trellix.sh")

    return {
        "client_id": os.environ["TRELLIX_CLIENT_ID"],
        "client_secret": os.environ["TRELLIX_CLIENT_SECRET"],
        "x_api_key": os.environ["TRELLIX_X_API_KEY"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Alerts
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@pytest.mark.trellix_live
class TestTrellixEdrAlertsLive:

    async def test_list_alerts_default(self, agent_context, trellix_config):
        """Fetch alerts with default parameters (no filters, 200 max rows)."""
        tool = TrellixEdrAlertsTool()
        result = await tool.execute(agent_context, {}, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        assert result.data["api_version"] == "v3"
        assert result.data["total_matched"] > 0, "Expected at least 1 alert"
        assert len(result.data["rows"]) > 0, "Expected preview rows"

    async def test_list_alerts_filtered_severity(self, agent_context, trellix_config):
        """Fetch alerts filtered by severity=s0."""
        tool = TrellixEdrAlertsTool()
        result = await tool.execute(agent_context, {
            "severity": "s0",
            "max_rows": 50,
            "lookback_hours": 12,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        for row in result.data["rows"]:
            assert str(row.get("Severity", "")).lower() == "s0", f"Unexpected severity: {row.get('Severity')}"

    async def test_list_alerts_hostname_filter(self, agent_context, trellix_config):
        """Fetch alerts filtered by hostname_contains."""
        tool = TrellixEdrAlertsTool()
        # Use lookback_hours=0 to get server default window (widest)
        result = await tool.execute(agent_context, {
            "hostname_contains": "PR",
            "max_rows": 20,
            "lookback_hours": 12,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        if result.data["total_matched"] > 0:
            for row in result.data["rows"]:
                host = str(row.get("Host_Name", ""))
                assert "pr" in host.lower(), f"Host_Name '{host}' does not contain 'PR'"

    async def test_list_alerts_export_csv(self, agent_context, trellix_config):
        """Fetch alerts with CSV export enabled."""
        tool = TrellixEdrAlertsTool()
        result = await tool.execute(agent_context, {
            "max_rows": 10,
            "export_csv": True,
            "lookback_hours": 12,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        # CSV should be auto-generated when rows_total > 0
        csv_file = result.data["artifacts"].get("csv_file")
        assert csv_file is not None, "Expected CSV artifact"

    async def test_list_alerts_sort_rank(self, agent_context, trellix_config):
        """Fetch alerts sorted by rank descending."""
        tool = TrellixEdrAlertsTool()
        result = await tool.execute(agent_context, {
            "max_rows": 10,
            "sort": "-rank",
            "lookback_hours": 12,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        if len(result.data["rows"]) >= 2:
            ranks = [row.get("Rank", 0) for row in result.data["rows"]]
            assert ranks == sorted(ranks, reverse=True), f"Ranks not descending: {ranks}"

    async def test_list_alerts_no_time_filter(self, agent_context, trellix_config):
        """Fetch alerts with lookback_hours=0 (omit from/to — server default window)."""
        tool = TrellixEdrAlertsTool()
        result = await tool.execute(agent_context, {
            "max_rows": 10,
            "lookback_hours": 0,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        assert result.data["total_matched"] > 0

    async def test_summary_has_expected_keys(self, agent_context, trellix_config):
        """Verify the summary block contains the expected structure."""
        tool = TrellixEdrAlertsTool()
        result = await tool.execute(agent_context, {
            "max_rows": 20,
            "lookback_hours": 12,
        }, trellix_config, {})

        assert result.status == "success"
        summary = result.data["summary"]
        assert "row_count" in summary
        assert "distinct" in summary
        assert "top" in summary
        assert "sample" in summary


# ═══════════════════════════════════════════════════════════════════════════════
# Threats
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@pytest.mark.trellix_live
class TestTrellixEdrThreatsLive:

    async def test_list_threats_default(self, agent_context, trellix_config):
        """List threats with default parameters."""
        tool = TrellixEdrThreatsTool()
        result = await tool.execute(agent_context, {
            "action": "list",
            "max_rows": 20,
            "lookback_hours": 168,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        assert result.data["total_matched"] > 0, "Expected at least 1 threat"
        # Threat IDs are numeric strings
        for row in result.data["rows"]:
            assert str(row.get("id", "")).isdigit(), f"Expected numeric threat ID, got: {row.get('id')}"

    async def test_list_threats_by_severity(self, agent_context, trellix_config):
        """List threats filtered by severity s4."""
        tool = TrellixEdrThreatsTool()
        result = await tool.execute(agent_context, {
            "action": "list",
            "severity": "s4",
            "max_rows": 10,
            "lookback_hours": 168,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        for row in result.data["rows"]:
            assert str(row.get("severity", "")).lower() == "s4", f"Unexpected severity: {row.get('severity')}"

    async def test_list_threats_by_hash(self, agent_context, trellix_config):
        """List threats filtered by SHA256 hash."""
        tool = TrellixEdrThreatsTool()
        # Use a known hash from the test data
        result = await tool.execute(agent_context, {
            "action": "list",
            "hash": "2198A7B58BCCB758036B969DDAE6CC2ECE07565E2659A7C541A313A0492231A3",
            "max_rows": 5,
            "lookback_hours": 168,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        if result.data["total_matched"] > 0:
            for row in result.data["rows"]:
                assert "2198a7b5" in str(row.get("hashes.sha256", "")).lower()

    async def test_threat_detail(self, agent_context, trellix_config):
        """Fetch a single threat by ID (discovered from list)."""
        tool = TrellixEdrThreatsTool()

        # First, list to get a threat ID
        list_result = await tool.execute(agent_context, {
            "action": "list", "max_rows": 1, "lookback_hours": 168,
        }, trellix_config, {})

        assert list_result.status == "success"
        assert len(list_result.data["rows"]) > 0, "Need at least 1 threat to test detail"
        threat_id = str(list_result.data["rows"][0]["id"])

        # Then fetch detail
        detail_result = await tool.execute(agent_context, {
            "action": "detail",
            "threat_id": threat_id,
        }, trellix_config, {})

        assert detail_result.status == "success", f"Failed: {detail_result.error}"
        assert detail_result.data["total_matched"] == 1
        # Detail row should have the same ID
        assert str(detail_result.data["rows"][0].get("id", "")) == threat_id

    async def test_affected_hosts(self, agent_context, trellix_config):
        """Fetch affected hosts for a threat."""
        tool = TrellixEdrThreatsTool()

        list_result = await tool.execute(agent_context, {
            "action": "list", "max_rows": 1, "lookback_hours": 168,
        }, trellix_config, {})
        threat_id = str(list_result.data["rows"][0]["id"])

        result = await tool.execute(agent_context, {
            "action": "affected_hosts",
            "threat_id": threat_id,
            "max_rows": 10,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        # Affected hosts should have hostname-like flattened fields
        if result.data["rows"]:
            row_keys = " ".join(str(k) for k in result.data["rows"][0])
            assert "hostname" in row_keys.lower() or "host" in row_keys.lower(), \
                f"Expected hostname field in: {list(result.data['rows'][0].keys())}"

    async def test_detections(self, agent_context, trellix_config):
        """Fetch detections for a threat."""
        tool = TrellixEdrThreatsTool()

        list_result = await tool.execute(agent_context, {
            "action": "list", "max_rows": 1, "lookback_hours": 168,
        }, trellix_config, {})
        threat_id = str(list_result.data["rows"][0]["id"])

        result = await tool.execute(agent_context, {
            "action": "detections",
            "threat_id": threat_id,
            "max_rows": 10,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        # Detections should have traceId or tags
        if result.data["rows"]:
            row_keys = " ".join(str(k) for k in result.data["rows"][0])
            has_trace = "traceId" in row_keys or "trace" in row_keys.lower()
            has_tags = "tags" in row_keys.lower()
            assert has_trace or has_tags, \
                f"Expected traceId or tags in: {list(result.data['rows'][0].keys())}"

    async def test_invalid_action(self, agent_context, trellix_config):
        """Invalid action should return failure."""
        tool = TrellixEdrThreatsTool()
        result = await tool.execute(agent_context, {
            "action": "nonexistent",
        }, trellix_config, {})

        assert result.status == "error"
        assert result.error is not None
        assert "INVALID_INPUT" in result.error.get("code", "")

    async def test_detail_missing_threat_id(self, agent_context, trellix_config):
        """detail action without threat_id should fail."""
        tool = TrellixEdrThreatsTool()
        result = await tool.execute(agent_context, {
            "action": "detail",
        }, trellix_config, {})

        assert result.status == "error"
        assert "INVALID_INPUT" in result.error.get("code", "")
