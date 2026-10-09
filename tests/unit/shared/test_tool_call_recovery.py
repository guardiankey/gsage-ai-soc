"""Unit tests for the tool-call recovery layer.

Covers the DSML dialect (Azure AI Foundry / DeepSeek leak observed in
production), the streaming parser (chunk fragmentation, passthrough safety,
bare pythonic calls) and the provider adapter defaults.
"""

from __future__ import annotations

import json
from typing import Iterable, List, Tuple

from agno.models.openai import OpenAIChat
from agno.models.response import ModelResponse
from openai.types.chat.chat_completion_chunk import (
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)

from src.shared.llm.tool_call_recovery import (
    DsmlDialect,
    GemmaPythonicDialect,
    NoOpDialect,
    QwenHermesDialect,
    RecoveringToolCallOpenAI,
    RecoveringToolCallVLLM,
    ToolCallStreamParser,
    build_dialect,
)

#: Exact leak shape reported in production (fullwidth bars U+FF5C + spaces).
DSML_SAMPLE = (
    "I'll start by discovering what file-analysis tools are available.\n"
    "\n"
    "<\uff5cDSML\uff5c calls>\n"
    "<\uff5cDSML\uff5c invoke name=\"search_tools\">\n"
    "<\uff5cDSML\uff5c parameter name=\"query\" string=\"true\">"
    "analyze attached CSV file data profiling</\uff5cDSML\uff5c parameter>\n"
    "</\uff5cDSML\uff5c invoke>\n"
    "<\uff5cDSML\uff5c invoke name=\"instruction_catalog\">\n"
    "<\uff5cDSML\uff5c parameter name=\"action\" string=\"true\">list"
    "</\uff5cDSML\uff5c parameter>\n"
    "</\uff5cDSML\uff5c invoke>\n"
    "</\uff5cDSML\uff5c calls>"
)

#: Compact ASCII-bar variant without spaces.
DSML_SAMPLE_ASCII = (
    "<|DSML| calls>"
    "<|DSML| invoke name=\"search_tools\">"
    "<|DSML| parameter name=\"query\" string=\"true\">dns test"
    "</|DSML| parameter>"
    "</|DSML| invoke>"
    "</|DSML| calls>"
)

#: Non-string parameter values must be JSON-coerced (string="false").
DSML_SAMPLE_JSON_VALUE = (
    "<|DSML| calls>"
    "<|DSML| invoke name=\"run_discovered_tool\">"
    "<|DSML| parameter name=\"tool_name\" string=\"true\">x</|DSML| parameter>"
    "<|DSML| parameter name=\"params\" string=\"false\">{\"count\": 42}"
    "</|DSML| parameter>"
    "</|DSML| invoke>"
    "</|DSML| calls>"
)


def _collect(outputs: Iterable[ModelResponse]) -> Tuple[str, List[ChoiceDeltaToolCall]]:
    """Split parser outputs into (forwarded text, recovered tool-call deltas)."""
    content = "".join(o.content or "" for o in outputs if o.content)
    calls: List[ChoiceDeltaToolCall] = []
    for o in outputs:
        if o.tool_calls:
            calls.extend(o.tool_calls)  # type: ignore[arg-type]
    return content, calls


def _feed_chunks(text: str, parser: ToolCallStreamParser, size: int) -> List[ModelResponse]:
    outputs: List[ModelResponse] = []
    for i in range(0, len(text), size):
        outputs.extend(parser.feed(ModelResponse(content=text[i : i + size])))
    outputs.extend(parser.flush())
    return outputs


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_build_dialect_registry() -> None:
    assert isinstance(build_dialect("dsml"), DsmlDialect)
    assert isinstance(build_dialect("deepseek"), DsmlDialect)
    assert isinstance(build_dialect("gemma"), GemmaPythonicDialect)
    assert isinstance(build_dialect("qwen"), QwenHermesDialect)
    assert isinstance(build_dialect("hermes"), QwenHermesDialect)
    assert isinstance(build_dialect(None), NoOpDialect)
    assert isinstance(build_dialect("none"), NoOpDialect)


# ---------------------------------------------------------------------------
# DSML dialect
# ---------------------------------------------------------------------------


def test_dsml_parse_block_user_sample() -> None:
    calls = DsmlDialect().parse_block(DSML_SAMPLE)
    assert [c.name for c in calls] == ["search_tools", "instruction_catalog"]
    assert calls[0].arguments == {"query": "analyze attached CSV file data profiling"}
    assert calls[1].arguments == {"action": "list"}


def test_dsml_parse_block_ascii_variant() -> None:
    calls = DsmlDialect().parse_block(DSML_SAMPLE_ASCII)
    assert [c.name for c in calls] == ["search_tools"]
    assert calls[0].arguments == {"query": "dns test"}


def test_dsml_parse_block_json_value() -> None:
    calls = DsmlDialect().parse_block(DSML_SAMPLE_JSON_VALUE)
    assert [c.name for c in calls] == ["run_discovered_tool"]
    assert calls[0].arguments == {"tool_name": "x", "params": {"count": 42}}


# ---------------------------------------------------------------------------
# Streaming parser
# ---------------------------------------------------------------------------


def test_dsml_stream_fragmented_recovery() -> None:
    parser = ToolCallStreamParser(build_dialect("dsml"), label="test")
    outputs = _feed_chunks(DSML_SAMPLE, parser, size=5)
    content, calls = _collect(outputs)
    # No markup must leak to the forwarded text.
    assert "DSML" not in content
    assert content.strip() == (
        "I'll start by discovering what file-analysis tools are available."
    )
    names = [tc.function.name for tc in calls]
    assert names == ["search_tools", "instruction_catalog"]
    args = [json.loads(tc.function.arguments) for tc in calls]
    assert args[0] == {"query": "analyze attached CSV file data profiling"}
    assert args[1] == {"action": "list"}


def test_dsml_stream_single_chunk() -> None:
    parser = ToolCallStreamParser(build_dialect("dsml"))
    outputs = list(parser.feed(ModelResponse(content=DSML_SAMPLE_ASCII)))
    outputs.extend(parser.flush())
    content, calls = _collect(outputs)
    assert "DSML" not in content
    assert content.strip() == ""
    assert [tc.function.name for tc in calls] == ["search_tools"]
    assert json.loads(calls[0].function.arguments) == {"query": "dns test"}


def test_dsml_unparseable_block_forwarded_verbatim() -> None:
    text = "<|DSML| calls> no structured content here </|DSML| calls>"
    parser = ToolCallStreamParser(build_dialect("dsml"))
    outputs = _feed_chunks(text, parser, size=7)
    content, calls = _collect(outputs)
    assert calls == []
    # Block could not be parsed -> must be forwarded verbatim (no data loss).
    assert "no structured content here" in content
    assert "DSML" in content


def test_dsml_invoke_without_wrapper_passthrough() -> None:
    # Documented limitation: only the outer wrapper is detected.  The text is
    # forwarded untouched (and flagged by the suspect-leak warning).
    text = "<|DSML| invoke name=\"search_tools\">"
    parser = ToolCallStreamParser(build_dialect("dsml"))
    outputs = _feed_chunks(text, parser, size=3)
    content, calls = _collect(outputs)
    assert calls == []
    assert "search_tools" in content


def test_native_tool_calls_pass_through_untouched() -> None:
    tc = ChoiceDeltaToolCall(
        index=0,
        id="call_1",
        type="function",
        function=ChoiceDeltaToolCallFunction(
            name="dns_lookup", arguments='{"domain": "example.com"}'
        ),
    )
    mr = ModelResponse(tool_calls=[tc])
    parser = ToolCallStreamParser(build_dialect("dsml"))
    outputs = list(parser.feed(mr))
    assert outputs == [mr]


def test_bare_pythonic_call_recovered_with_dsml_dialect() -> None:
    parser = ToolCallStreamParser(build_dialect("dsml"))
    text = 'Vou buscar as campanhas. search_tools(query="egoi campaign")'
    outputs = _feed_chunks(text, parser, size=6)
    content, calls = _collect(outputs)
    assert "search_tools" not in content
    assert content.strip() == "Vou buscar as campanhas."
    assert [tc.function.name for tc in calls] == ["search_tools"]
    assert json.loads(calls[0].function.arguments) == {"query": "egoi campaign"}


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def test_recovering_tool_call_openai_defaults() -> None:
    model = RecoveringToolCallOpenAI(id="test-model", api_key="k")
    assert isinstance(model, OpenAIChat)
    assert model.name == "RecoveringToolCallOpenAI"
    assert model.provider == "OpenAI"
    assert model.tool_call_dialect == "dsml"


def test_recovering_tool_call_vllm_defaults_unchanged() -> None:
    model = RecoveringToolCallVLLM(id="test-model", api_key="k")
    assert model.tool_call_dialect == "gemma"
    assert model.force_non_streaming is False
