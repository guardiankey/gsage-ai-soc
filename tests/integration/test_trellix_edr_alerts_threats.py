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
from typing import cast

import pytest

from src.mcp_server.tools.soc.edr.trellix.trellix_edr_alerts import TrellixEdrAlertsTool
from src.mcp_server.tools.soc.edr.trellix.trellix_edr_threats import TrellixEdrThreatsTool
from src.shared.security.context import AgentContext, RequestSource


# ── Helpers ──────────────────────────────────────────────────────────────────


def _require_data(result: object) -> dict:
    """Assert result.data is not None and return it (type narrow)."""
    from src.mcp_server.tools.base import ToolResult
    assert isinstance(result, ToolResult), f"Expected ToolResult, got {type(result)}"
    assert result.data is not None, f"result.data is None: {result.error}"
    return cast(dict, result.data)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def agent_context() -> AgentContext:
    """Minimal agent context for tool execution (org/user IDs are placeholders)."""
    return AgentContext(
        org_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
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
        data = _require_data(result)
        assert data["api_version"] == "v3"
        assert data["total_matched"] > 0, "Expected at least 1 alert"
        assert len(data["rows"]) > 0, "Expected preview rows"

    async def test_list_alerts_filtered_severity(self, agent_context, trellix_config):
        """Fetch alerts filtered by severity=s0."""
        tool = TrellixEdrAlertsTool()
        result = await tool.execute(agent_context, {
            "severity": "s0",
            "max_rows": 50,
            "lookback_hours": 12,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        data = _require_data(result)
        for row in data["rows"]:
            assert str(row.get("Severity", "")).lower() == "s0"

    async def test_list_alerts_hostname_filter(self, agent_context, trellix_config):
        """Fetch alerts filtered by hostname_contains."""
        tool = TrellixEdrAlertsTool()
        result = await tool.execute(agent_context, {
            "hostname_contains": "PR",
            "max_rows": 20,
            "lookback_hours": 12,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        data = _require_data(result)
        if data["total_matched"] > 0:
            for row in data["rows"]:
                host = str(row.get("Host_Name", ""))
                assert "pr" in host.lower(), f"Host_Name '{host}' does not contain 'PR'"

    async def test_list_alerts_export_csv(self, agent_context, trellix_config):
        """Fetch alerts with CSV export enabled."""
        tool = TrellixEdrAlertsTool()
        result = await tool.execute(agent_context, {
            "max_rows": 150,
            "export_csv": True,
            "lookback_hours": 12,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        data = _require_data(result)
        csv_file = data["artifacts"].get("csv_file")
        assert csv_file is not None, "Expected CSV artifact when rows > 100"

    async def test_list_alerts_no_time_filter(self, agent_context, trellix_config):
        """Fetch alerts with lookback_hours=0 (omit from/to)."""
        tool = TrellixEdrAlertsTool()
        result = await tool.execute(agent_context, {
            "max_rows": 10,
            "lookback_hours": 0,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        data = _require_data(result)
        assert data["total_matched"] > 0

    async def test_summary_has_expected_keys(self, agent_context, trellix_config):
        """Verify the summary block contains the expected structure."""
        tool = TrellixEdrAlertsTool()
        result = await tool.execute(agent_context, {
            "max_rows": 20,
            "lookback_hours": 12,
        }, trellix_config, {})

        assert result.status == "success"
        data = _require_data(result)
        summary = data["summary"]
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
        data = _require_data(result)
        assert data["total_matched"] > 0, "Expected at least 1 threat"
        for row in data["rows"]:
            assert str(row.get("id", "")).isdigit()

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
        data = _require_data(result)
        for row in data["rows"]:
            assert str(row.get("severity", "")).lower() == "s4"

    async def test_list_threats_by_hash(self, agent_context, trellix_config):
        """List threats filtered by SHA256 hash."""
        tool = TrellixEdrThreatsTool()
        result = await tool.execute(agent_context, {
            "action": "list",
            "hash": "2198A7B58BCCB758036B969DDAE6CC2ECE07565E2659A7C541A313A0492231A3",
            "max_rows": 5,
            "lookback_hours": 168,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        data = _require_data(result)
        if data["total_matched"] > 0:
            for row in data["rows"]:
                assert "2198a7b5" in str(row.get("hashes.sha256", "")).lower()

    async def test_threat_detail(self, agent_context, trellix_config):
        """Fetch a single threat by ID (discovered from list)."""
        tool = TrellixEdrThreatsTool()

        list_result = await tool.execute(agent_context, {
            "action": "list", "max_rows": 1, "lookback_hours": 168,
        }, trellix_config, {})

        assert list_result.status == "success"
        list_data = _require_data(list_result)
        assert len(list_data["rows"]) > 0
        threat_id = str(list_data["rows"][0]["id"])

        detail_result = await tool.execute(agent_context, {
            "action": "detail",
            "threat_id": threat_id,
        }, trellix_config, {})

        assert detail_result.status == "success", f"Failed: {detail_result.error}"
        detail_data = _require_data(detail_result)
        assert detail_data["total_matched"] == 1
        assert str(detail_data["rows"][0].get("id", "")) == threat_id

    async def test_affected_hosts(self, agent_context, trellix_config):
        """Fetch affected hosts for a threat."""
        tool = TrellixEdrThreatsTool()

        list_result = await tool.execute(agent_context, {
            "action": "list", "max_rows": 1, "lookback_hours": 168,
        }, trellix_config, {})
        list_data = _require_data(list_result)
        threat_id = str(list_data["rows"][0]["id"])

        result = await tool.execute(agent_context, {
            "action": "affected_hosts",
            "threat_id": threat_id,
            "max_rows": 10,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        data = _require_data(result)
        if data["rows"]:
            row_keys = " ".join(str(k) for k in data["rows"][0])
            assert "hostname" in row_keys.lower() or "host" in row_keys.lower()

    async def test_detections(self, agent_context, trellix_config):
        """Fetch detections for a threat."""
        tool = TrellixEdrThreatsTool()

        list_result = await tool.execute(agent_context, {
            "action": "list", "max_rows": 1, "lookback_hours": 168,
        }, trellix_config, {})
        list_data = _require_data(list_result)
        threat_id = str(list_data["rows"][0]["id"])

        result = await tool.execute(agent_context, {
            "action": "detections",
            "threat_id": threat_id,
            "max_rows": 10,
        }, trellix_config, {})

        assert result.status == "success", f"Failed: {result.error}"
        data = _require_data(result)
        if data["rows"]:
            row_keys = " ".join(str(k) for k in data["rows"][0])
            has_trace = "traceId" in row_keys or "trace" in row_keys.lower()
            has_tags = "tags" in row_keys.lower()
            assert has_trace or has_tags

    async def test_invalid_action(self, agent_context, trellix_config):
        """Invalid action should return failure."""
        tool = TrellixEdrThreatsTool()
        result = await tool.execute(agent_context, {
            "action": "nonexistent",
        }, trellix_config, {})

        assert result.status == "error"
        assert result.error is not None
        assert "INVALID_INPUT" in (result.error.get("code") or "")

    async def test_detail_missing_threat_id(self, agent_context, trellix_config):
        """detail action without threat_id should fail."""
        tool = TrellixEdrThreatsTool()
        result = await tool.execute(agent_context, {
            "action": "detail",
        }, trellix_config, {})

        assert result.status == "error"
        assert result.error is not None
        assert "INVALID_INPUT" in (result.error.get("code") or "")
