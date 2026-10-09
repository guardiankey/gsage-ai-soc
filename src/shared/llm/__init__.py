"""LLM provider helpers and adapters for gSage.

Currently hosts the tool-call recovery layer
(:mod:`src.shared.llm.tool_call_recovery`) that recovers tool calls leaked as
plain text by buggy/disabled server-side streaming tool-call parsers (Gemma
pythonic, Qwen/Hermes JSON, DeepSeek DSML, …), wrapped into per-provider Agno
model adapters for ``vllm`` and ``openai``.
"""
