"""LLM provider helpers and adapters for gSage.

Currently hosts the vLLM streaming tool-call adapter that recovers tool calls
leaked as plain text by buggy/disabled server-side streaming tool-call parsers
(Gemma pythonic, Qwen/Hermes JSON, …; see :mod:`src.shared.llm.vllm_recovering`).
"""
