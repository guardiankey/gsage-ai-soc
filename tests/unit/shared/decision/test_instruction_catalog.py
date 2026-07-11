"""Unit tests for Instruction Catalog tool and helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.mcp_server.tools.enterprise.instruction_catalog.instruction_catalog import (
    InstructionCatalogTool,
    _load_index,
    _load_yaml,
    _build_list_response,
    _search_in_content,
)

_DEFINITIONS_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / (
    "src/mcp_server/tools/enterprise/instruction_catalog/definitions"
)


@pytest.fixture
def index() -> dict:
    return _load_index()


@pytest.fixture
def tool() -> InstructionCatalogTool:
    return InstructionCatalogTool()


class TestToolMetadata:
    """Tests for tool class variables."""

    def test_name(self, tool):
        assert tool.name == "instruction_catalog"

    def test_core_tool(self, tool):
        assert tool.core_tool is True

    def test_category(self, tool):
        assert tool.category == "utility"

    def test_params_schema(self, tool):
        assert "action" in tool.params_schema["properties"]
        assert "list" in tool.params_schema["properties"]["action"]["enum"]
        assert "get" in tool.params_schema["properties"]["action"]["enum"]
        assert "search" in tool.params_schema["properties"]["action"]["enum"]
        assert "resolve" in tool.params_schema["properties"]["action"]["enum"]


class TestIndex:
    """Tests for INDEX.yaml loading."""

    def test_index_has_instructions(self, index):
        assert len(index["instructions"]) >= 4

    def test_index_required_fields(self, index):
        for entry in index["instructions"]:
            assert "id" in entry
            assert "title" in entry
            assert "summary" in entry
            assert "category" in entry
            assert "file" in entry

    def test_index_ids_unique(self, index):
        ids = [e["id"] for e in index["instructions"]]
        assert len(ids) == len(set(ids))


class TestLoadYAML:
    """Tests for YAML loading helpers."""

    def test_load_whois(self):
        data = _load_yaml("whois_extraction")
        assert data is not None
        inst = data["instruction"]
        assert inst["id"] == "whois_extraction"
        assert len(inst["assets"]) >= 5
        assert "applies_when" in inst

    def test_load_contratacao_tic(self):
        data = _load_yaml("contratacao_tic")
        assert data is not None
        inst = data["instruction"]
        assert inst["category"] == "licitacoes"
        assert inst["priority"] == 100
        assert len(inst["asset_refs"]) >= 2

    def test_load_lgpd(self):
        data = _load_yaml("lgpd_contratacao_checklist")
        assert data is not None
        inst = data["instruction"]
        assert inst["category"] == "compliance"
        # LGPD instruction should have checklist assets
        asset_types = [a["type"] for a in inst["assets"]]
        assert "checklist" in asset_types

    def test_load_nonexistent(self):
        data = _load_yaml("nonexistent_id")
        assert data is None


class TestBuildListResponse:
    """Tests for list filtering."""

    def test_no_filters(self, index):
        instructions, count = _build_list_response(index)
        assert count == len(index["instructions"])

    def test_filter_category(self, index):
        instructions, count = _build_list_response(index, category="soc")
        assert count >= 1
        for inst in instructions:
            assert inst["category"] == "soc"

    def test_filter_query(self, index):
        instructions, count = _build_list_response(index, query="LGPD")
        assert count >= 1
        # All results should have LGPD in title, summary, or tags
        for inst in instructions:
            found = (
                "LGPD" in (inst.get("title") or "").upper()
                or "LGPD" in (inst.get("summary") or "").upper()
                or any("LGPD" in (t or "").upper() for t in inst.get("tags", []))
            )
            assert found

    def test_filter_nonexistent_category(self, index):
        instructions, count = _build_list_response(index, category="nonexistent")
        assert count == 0


class TestSearchInContent:
    """Tests for full-text search in instruction content."""

    def test_search_found_in_summary(self):
        assert _search_in_content("whois_extraction", "whois") is True

    def test_search_found_in_asset(self):
        assert _search_in_content("whois_extraction", "INDICADOR") is True

    def test_search_not_found(self):
        assert _search_in_content("whois_extraction", "xyznonexistent123") is False

    def test_search_case_insensitive(self):
        assert _search_in_content("whois_extraction", "indicador") is True
        assert _search_in_content("whois_extraction", "WHOIS") is True

    def test_search_lgpd(self):
        assert _search_in_content("lgpd_contratacao_checklist", "LGPD") is True
        assert _search_in_content("lgpd_contratacao_checklist", "tratamento") is True


class TestAllInstructionsValid:
    """Validate all instruction YAMLs can be loaded and have required fields."""

    def test_all_loadable_and_valid(self):
        index = _load_index()
        for entry in index["instructions"]:
            inst_id = entry["id"]
            data = _load_yaml(inst_id)
            assert data is not None, f"Failed to load {inst_id}"
            inst = data.get("instruction", {})
            assert inst.get("id") == inst_id, f"ID mismatch in {inst_id}"
            assert "title" in inst, f"Missing title in {inst_id}"
            assert "summary" in inst, f"Missing summary in {inst_id}"
            assert "assets" in inst, f"Missing assets in {inst_id}"
            assert len(inst["assets"]) >= 1, f"Empty assets in {inst_id}"
