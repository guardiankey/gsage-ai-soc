"""Tool-call recovery layer for Agno model streams.

Recovers tool calls that providers leak as plain text during streaming (see
:mod:`src.shared.llm.tool_call_recovery.dialects` for the supported text
shapes), wrapped into per-provider Agno model adapters:

* :class:`RecoveringToolCallVLLM` — self-hosted vLLM.
* :class:`RecoveringToolCallOpenAI` — OpenAI-compatible endpoints (e.g.
  Azure AI Foundry serving DeepSeek models).

Import the public API from this package
(``from src.shared.llm.tool_call_recovery import ...``) instead of the
submodules.
"""

from src.shared.llm.tool_call_recovery.adapters import (
    RecoveringToolCallOpenAI,
    RecoveringToolCallVLLM,
)
from src.shared.llm.tool_call_recovery.dialects import (
    DsmlDialect,
    GemmaPythonicDialect,
    NoOpDialect,
    QwenHermesDialect,
    ToolCallData,
    ToolCallDialect,
    build_dialect,
)
from src.shared.llm.tool_call_recovery.parser import (
    ToolCallStreamParser,
    sanitize_tool_call_arguments,
)

__all__ = [
    "ToolCallData",
    "ToolCallDialect",
    "GemmaPythonicDialect",
    "QwenHermesDialect",
    "DsmlDialect",
    "NoOpDialect",
    "ToolCallStreamParser",
    "RecoveringToolCallVLLM",
    "RecoveringToolCallOpenAI",
    "build_dialect",
    "sanitize_tool_call_arguments",
]
