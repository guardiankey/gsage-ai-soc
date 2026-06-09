"""vLLM adapter that recovers tool calls leaked as plain text during streaming.

Background
----------
Some open-source model + vLLM tool-call-parser combinations (notably
``google/gemma-*`` with ``--tool-call-parser gemma4``) only emit proper
OpenAI ``tool_calls`` when ``stream=false``.  In streaming mode the parser
buffer is never flushed before the special tokens are forwarded, so the raw
tool-call markup escapes to the client inside ``content`` deltas, e.g.::

    <|tool_call>call:dns_lookup{domain:guardiankey.io}<tool_call|>

Agno only executes tools when ``assistant_message.tool_calls`` is populated
(via :meth:`Model.parse_tool_calls`), so a tool call that arrives as plain
text is silently ignored.

This module provides a defensive, client-side recovery layer:

* :class:`ToolCallStreamParser` — an incremental state machine that scans
  streamed ``content`` for tool-call blocks, tolerant to SSE chunk
  fragmentation (a marker may be split across several deltas), and converts
  recognised blocks into synthetic OpenAI ``ChoiceDeltaToolCall`` objects so
  the rest of the Agno pipeline behaves exactly as with native tool calls.
* :class:`ToolCallDialect` — a pluggable contract so additional open-source
  tool-calling dialects (Hermes, Llama, …) can be added later.
* :class:`GemmaToolCallVLLM` — an Agno :class:`~agno.models.vllm.VLLM`
  subclass that wires the parser into ``invoke_stream``/``ainvoke_stream`` and
  optionally forces non-streaming requests as an extra robustness fallback.

Design notes
------------
* Native OpenAI ``tool_calls`` are always passed through untouched, so this
  layer stays correct even after the upstream vLLM bug is fixed.
* All parser state lives in the generator/parser instance created per
  request, never on the model instance — safe under concurrency even if the
  model object were shared.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Protocol
from uuid import uuid4

from openai.types.chat.chat_completion_chunk import (
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)

from agno.models.response import ModelResponse
from agno.models.vllm import VLLM
from agno.utils.log import log_debug, log_warning

__all__ = [
    "ToolCallData",
    "ToolCallDialect",
    "GemmaPythonicDialect",
    "NoOpDialect",
    "ToolCallStreamParser",
    "GemmaToolCallVLLM",
    "build_dialect",
]


# ---------------------------------------------------------------------------
# Parsed tool-call representation
# ---------------------------------------------------------------------------


@dataclass
class ToolCallData:
    """A tool call recovered from raw model text."""

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Dialect contract
# ---------------------------------------------------------------------------


class ToolCallDialect(Protocol):
    """Contract for a tool-calling text dialect.

    Implementations describe how a given open-source model encodes tool calls
    inside plain text so :class:`ToolCallStreamParser` can detect and decode
    them incrementally.
    """

    #: Marker strings that open a tool-call block (any may appear).
    start_markers: List[str]
    #: Marker strings that close a tool-call block (any may appear).
    end_markers: List[str]

    def parse_block(self, body: str) -> List[ToolCallData]:
        """Parse the text *between* start and end markers into tool calls."""
        ...


def _longest_partial_suffix(buffer: str, markers: List[str]) -> int:
    """Return the length of the longest suffix of *buffer* that is a strict
    prefix of any marker in *markers*.

    Used to hold back the tail of a passthrough buffer that might turn out to
    be the beginning of a marker once more chunks arrive.
    """
    max_len = 0
    for marker in markers:
        # Check all prefixes of the marker shorter than the marker itself.
        limit = min(len(buffer), len(marker) - 1)
        for size in range(limit, 0, -1):
            if buffer.endswith(marker[:size]):
                max_len = max(max_len, size)
                break
    return max_len


def _find_first_marker(buffer: str, markers: List[str], start: int = 0) -> tuple[int, str]:
    """Return ``(index, marker)`` of the earliest marker found in *buffer*.

    Returns ``(-1, "")`` when no full marker is present.
    """
    best_idx = -1
    best_marker = ""
    for marker in markers:
        idx = buffer.find(marker, start)
        if idx != -1 and (best_idx == -1 or idx < best_idx):
            best_idx = idx
            best_marker = marker
    return best_idx, best_marker


# ---------------------------------------------------------------------------
# Gemma 4 "pythonic" dialect
# ---------------------------------------------------------------------------


class GemmaPythonicDialect:
    """Dialect for Gemma 4 tool calls leaked by the vLLM ``gemma4`` parser.

    Recognised shape (markers may vary slightly across builds)::

        <|tool_call>call:NAME{key:value, key2:value2}<tool_call|>

    String values are wrapped in the escaped-quote token ``<|"|>`` and other
    values may be unquoted (``guardiankey.io``), numeric (``42``), or boolean
    (``true``/``false``).
    """

    start_markers = ["<|tool_call>", "<tool_call>", "<|tool_call|>"]
    end_markers = ["<tool_call|>", "</tool_call>", "<|/tool_call|>"]

    #: Token vLLM emits in place of a literal double quote.
    _QUOTE_TOKEN = '<|"|>'
    _CALL_RE = re.compile(r"call\s*:\s*([A-Za-z_][A-Za-z0-9_\-\.]*)\s*\{", re.DOTALL)

    def parse_block(self, body: str) -> List[ToolCallData]:
        text = body.replace(self._QUOTE_TOKEN, '"').strip()
        calls: List[ToolCallData] = []
        for match in self._CALL_RE.finditer(text):
            name = match.group(1)
            args_str, _ = _extract_braced(text, match.end() - 1)
            arguments = _parse_pythonic_args(args_str) if args_str is not None else {}
            calls.append(ToolCallData(name=name, arguments=arguments))
        return calls


class NoOpDialect:
    """Dialect that never matches — used when text parsing is disabled.

    With no markers the parser becomes a transparent passthrough (still
    forwarding native ``tool_calls`` untouched).
    """

    start_markers: List[str] = []
    end_markers: List[str] = []

    def parse_block(self, body: str) -> List[ToolCallData]:  # pragma: no cover - never called
        return []


def build_dialect(name: Optional[str]) -> ToolCallDialect:
    """Resolve a dialect by name (``"gemma"`` or ``None``/``"none"``)."""
    if name is None:
        return NoOpDialect()
    key = name.strip().lower()
    if key in ("", "none", "off", "disabled"):
        return NoOpDialect()
    if key in ("gemma", "gemma4", "gemma_pythonic", "pythonic"):
        return GemmaPythonicDialect()
    log_warning(f"Unknown vLLM tool-call dialect '{name}', falling back to passthrough")
    return NoOpDialect()


# ---------------------------------------------------------------------------
# Tolerant value/argument parsing helpers
# ---------------------------------------------------------------------------


def _extract_braced(text: str, open_idx: int) -> tuple[Optional[str], int]:
    """Extract the substring inside a balanced ``{...}`` starting at *open_idx*.

    Returns ``(inner, end_index)`` where ``end_index`` points past the closing
    brace, or ``(remaining, len(text))`` when no closing brace is present
    (best-effort for truncated/streamed input).
    """
    assert text[open_idx] == "{"
    depth = 0
    in_str: Optional[str] = None
    i = open_idx
    while i < len(text):
        ch = text[i]
        if in_str is not None:
            if ch == in_str:
                in_str = None
        elif ch in ('"', "'"):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i], i + 1
        i += 1
    # Unbalanced — return best effort (everything after the opening brace).
    return text[open_idx + 1 :], len(text)


def _split_top_level(s: str, sep: str) -> List[str]:
    """Split *s* on *sep* ignoring separators inside quotes or brackets."""
    parts: List[str] = []
    depth = 0
    in_str: Optional[str] = None
    current: List[str] = []
    for ch in s:
        if in_str is not None:
            if ch == in_str:
                in_str = None
            current.append(ch)
        elif ch in ('"', "'"):
            in_str = ch
            current.append(ch)
        elif ch in "{[(":
            depth += 1
            current.append(ch)
        elif ch in "}])":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _coerce_value(raw: str) -> Any:
    """Best-effort coercion of a Gemma argument value string to a Python type."""
    v = raw.strip()
    if not v:
        return ""
    # Try strict JSON first (handles quoted strings, numbers, arrays, objects).
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        pass
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("none", "null"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    # Strip a single pair of surrounding quotes if present.
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    return v


def _parse_pythonic_args(args_str: str) -> Dict[str, Any]:
    """Parse a ``key:value, key2:value2`` Gemma argument body into a dict."""
    arguments: Dict[str, Any] = {}
    for pair in _split_top_level(args_str, ","):
        pair = pair.strip()
        if not pair:
            continue
        kv = _split_top_level(pair, ":")
        if len(kv) < 2:
            continue
        key = kv[0].strip().strip("\"'")
        value = ":".join(kv[1:]).strip()
        if not key:
            continue
        arguments[key] = _coerce_value(value)
    return arguments


# ---------------------------------------------------------------------------
# Synthetic tool-call construction
# ---------------------------------------------------------------------------


def _make_delta_tool_call(index: int, name: str, arguments: Any) -> ChoiceDeltaToolCall:
    """Build a synthetic OpenAI streaming tool-call delta.

    ``arguments`` may be a dict (serialised to JSON) or an already-serialised
    JSON string (passed through).
    """
    if isinstance(arguments, str):
        args_json = arguments
    else:
        try:
            args_json = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            args_json = "{}"
    return ChoiceDeltaToolCall(
        index=index,
        id=f"call_{uuid4().hex[:24]}",
        type="function",
        function=ChoiceDeltaToolCallFunction(name=name, arguments=args_json),
    )


def _dict_tool_calls_to_deltas(tool_calls: List[Dict[str, Any]]) -> List[ChoiceDeltaToolCall]:
    """Convert non-streaming tool-call dicts into streaming delta objects."""
    deltas: List[ChoiceDeltaToolCall] = []
    for index, tc in enumerate(tool_calls):
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = fn.get("name") or ""
        arguments = fn.get("arguments", "")
        delta = _make_delta_tool_call(index, name, arguments)
        # Preserve the provider-supplied id when available.
        tc_id = tc.get("id") if isinstance(tc, dict) else None
        if tc_id:
            delta.id = tc_id
        deltas.append(delta)
    return deltas


# ---------------------------------------------------------------------------
# Incremental stream parser (state machine)
# ---------------------------------------------------------------------------


class _State:
    PASSTHROUGH = "passthrough"
    IN_TOOL_CALL = "in_tool_call"


class ToolCallStreamParser:
    """Incremental parser that recovers tool calls from streamed text.

    Feed each :class:`ModelResponse` delta via :meth:`feed`; call
    :meth:`flush` once after the stream ends.  Both are generators of
    :class:`ModelResponse` objects to be re-yielded downstream.

    Resilient to:
      * markers split across multiple SSE chunks (holdback buffering);
      * multiple tool-call blocks in one stream (parallel/sequential calls);
      * incomplete trailing blocks (best-effort parse, else raw fallback).
    """

    def __init__(self, dialect: ToolCallDialect) -> None:
        self._dialect = dialect
        self._state = _State.PASSTHROUGH
        self._buffer = ""
        self._tool_index = 0
        self._enabled = bool(getattr(dialect, "start_markers", None))

    # -- public API ------------------------------------------------------

    def feed(self, model_response: ModelResponse) -> Iterator[ModelResponse]:
        """Process one streamed delta, yielding transformed deltas."""
        # Always pass native structured tool calls straight through.
        if model_response.tool_calls:
            yield model_response
            return

        content = model_response.content
        # Forward any non-content payload (usage, reasoning, role, …) intact.
        if not isinstance(content, str) or content == "":
            yield model_response
            return

        if not self._enabled:
            yield model_response
            return

        # Strip the content from the original delta but preserve its other
        # fields (token usage, reasoning_content, provider_data, …).
        if _has_non_content_payload(model_response):
            passthrough = _clone_without_content(model_response)
            yield passthrough

        self._buffer += content
        yield from self._drain()

    def flush(self) -> Iterator[ModelResponse]:
        """Flush any buffered text after the stream ends."""
        if not self._buffer:
            return
        if self._state == _State.IN_TOOL_CALL:
            # Try a best-effort parse of an unterminated block.
            calls = self._safe_parse(self._buffer)
            if calls:
                yield self._emit_tool_calls(calls)
            else:
                log_warning(
                    "vLLM tool-call parser: unterminated tool-call block at end "
                    f"of stream, forwarding as text ({len(self._buffer)} chars)"
                )
                yield ModelResponse(content=self._buffer)
        else:
            # Any held-back passthrough tail is safe to emit now.
            yield ModelResponse(content=self._buffer)
        self._buffer = ""

    # -- internals -------------------------------------------------------

    def _drain(self) -> Iterator[ModelResponse]:
        """Consume as much of the buffer as can be decided right now."""
        while True:
            if self._state == _State.PASSTHROUGH:
                idx, marker = _find_first_marker(self._buffer, self._dialect.start_markers)
                if idx == -1:
                    # Emit everything that cannot be the prefix of a marker.
                    hold = _longest_partial_suffix(self._buffer, self._dialect.start_markers)
                    safe = self._buffer[: len(self._buffer) - hold]
                    if safe:
                        yield ModelResponse(content=safe)
                    self._buffer = self._buffer[len(self._buffer) - hold :]
                    return
                # Emit text before the marker, then enter the tool-call block.
                if idx > 0:
                    yield ModelResponse(content=self._buffer[:idx])
                self._buffer = self._buffer[idx + len(marker) :]
                self._state = _State.IN_TOOL_CALL
                continue

            # IN_TOOL_CALL
            idx, marker = _find_first_marker(self._buffer, self._dialect.end_markers)
            if idx == -1:
                # Wait for more data — keep the whole block buffered.
                return
            block = self._buffer[:idx]
            self._buffer = self._buffer[idx + len(marker) :]
            self._state = _State.PASSTHROUGH
            calls = self._safe_parse(block)
            if calls:
                yield self._emit_tool_calls(calls)
            else:
                log_warning(
                    "vLLM tool-call parser: failed to parse tool-call block, "
                    "forwarding as text"
                )
                yield ModelResponse(content=block)
            continue

    def _safe_parse(self, block: str) -> List[ToolCallData]:
        try:
            return self._dialect.parse_block(block)
        except Exception as exc:  # noqa: BLE001 - best-effort, never crash the stream
            log_warning(f"vLLM tool-call parser error: {exc}")
            return []

    def _emit_tool_calls(self, calls: List[ToolCallData]) -> ModelResponse:
        deltas: List[ChoiceDeltaToolCall] = []
        for call in calls:
            deltas.append(_make_delta_tool_call(self._tool_index, call.name, call.arguments))
            self._tool_index += 1
        log_debug(
            f"vLLM tool-call parser recovered {len(deltas)} tool call(s) from text: "
            f"{[c.name for c in calls]}"
        )
        return ModelResponse(tool_calls=deltas)  # type: ignore[arg-type]


def _has_non_content_payload(mr: ModelResponse) -> bool:
    return any(
        [
            mr.role,
            mr.reasoning_content,
            mr.redacted_reasoning_content,
            mr.response_usage,
            mr.provider_data,
            mr.audio,
            mr.images,
            mr.videos,
            mr.citations,
        ]
    )


def _clone_without_content(mr: ModelResponse) -> ModelResponse:
    """Return a shallow copy of *mr* with ``content`` removed."""
    clone = ModelResponse(
        role=mr.role,
        reasoning_content=mr.reasoning_content,
        redacted_reasoning_content=mr.redacted_reasoning_content,
        response_usage=mr.response_usage,
        provider_data=mr.provider_data,
        audio=mr.audio,
        images=mr.images,
        videos=mr.videos,
        citations=mr.citations,
    )
    return clone


# ---------------------------------------------------------------------------
# Agno model subclass
# ---------------------------------------------------------------------------


@dataclass
class GemmaToolCallVLLM(VLLM):
    """vLLM model that recovers text-leaked tool calls during streaming.

    Parameters
    ----------
    tool_call_dialect:
        Name of the text dialect to detect (``"gemma"`` or ``None`` to
        disable text parsing while keeping native passthrough).
    force_non_streaming:
        When ``True``, streaming requests are internally served by a single
        non-streaming call (where vLLM emits correct ``tool_calls``) and then
        replayed as one delta.  Use as a robustness fallback when the parser
        is not sufficient.
    """

    name: str = "GemmaToolCallVLLM"
    provider: str = "VLLM"

    tool_call_dialect: Optional[str] = "gemma"
    force_non_streaming: bool = False

    def _new_parser(self) -> ToolCallStreamParser:
        return ToolCallStreamParser(build_dialect(self.tool_call_dialect))

    def _normalize_nonstream_response(self, model_response: ModelResponse) -> ModelResponse:
        """Convert a non-streaming ModelResponse into streaming-shaped deltas."""
        if model_response.tool_calls:
            model_response.tool_calls = _dict_tool_calls_to_deltas(  # type: ignore[assignment]
                list(model_response.tool_calls)
            )
        return model_response

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        parser = self._new_parser()
        if self.force_non_streaming:
            model_response = self.invoke(*args, **kwargs)
            yield from parser.feed(self._normalize_nonstream_response(model_response))
            yield from parser.flush()
            return
        for delta in super().invoke_stream(*args, **kwargs):
            yield from parser.feed(delta)
        yield from parser.flush()

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        parser = self._new_parser()
        if self.force_non_streaming:
            model_response = await self.ainvoke(*args, **kwargs)
            for out in parser.feed(self._normalize_nonstream_response(model_response)):
                yield out
            for out in parser.flush():
                yield out
            return
        async for delta in super().ainvoke_stream(*args, **kwargs):
            for out in parser.feed(delta):
                yield out
        for out in parser.flush():
            yield out
