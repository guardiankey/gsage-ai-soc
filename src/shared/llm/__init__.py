"""LLM provider helpers and adapters for gSage.

Currently hosts the vLLM/Gemma streaming tool-call adapter that recovers
tool calls leaked as plain text by buggy server-side streaming tool-call
parsers (see :mod:`src.shared.llm.vllm_gemma`).
"""
