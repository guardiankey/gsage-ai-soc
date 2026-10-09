"""Incremental tool-call recovery parser shared by all provider adapters.

Wraps any Agno model stream and converts tool calls that were leaked as plain
text (see dialects in :mod:`...dialects`) back into synthetic OpenAI
``tool_calls`` deltas, so the rest of the Agno pipeline behaves exactly as
with native tool calls.  Also hosts the streamed-argument sanitizer used by
the unknown-tool patch in the backend API.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List, Optional
from uuid import uuid4

from openai.types.chat.chat_completion_chunk import (
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)

from agno.models.response import ModelResponse
from agno.utils.log import log_debug, log_warning

from src.shared.llm.tool_call_recovery.dialects import (
    ToolCallData,
    ToolCallDialect,
    _coerce_value,
    _extract_braced,
    _split_top_level,
)


# ---------------------------------------------------------------------------
# Marker scanning helpers (streaming holdback)
# ---------------------------------------------------------------------------


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
# Bare pythonic call recovery (marker-less ``NAME(args)``)
# ---------------------------------------------------------------------------


def _extract_parenthesized(text: str, open_idx: int) -> tuple[str, Optional[int]]:
    """Extract the substring inside a balanced ``(...)`` starting at *open_idx*.

    Returns ``(inner, end_index)`` where ``end_index`` points past the closing
    parenthesis.  When the closing parenthesis has not arrived yet (streamed,
    truncated input) returns ``(partial_inner, None)`` so the caller can decide
    to wait for more data.
    """
    assert text[open_idx] == "("
    depth = 0
    in_str: Optional[str] = None
    i = open_idx
    while i < len(text):
        ch = text[i]
        if in_str is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in ('"', "'"):
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i], i + 1
        i += 1
    return text[open_idx + 1 :], None


def _parse_kwargs(args_str: str) -> Dict[str, Any]:
    """Parse a pythonic ``key=value, key2=value2`` argument body into a dict.

    Mirrors :func:`...dialects._parse_pythonic_args` but for the ``=``
    keyword-argument syntax used by bare pythonic calls
    (``search_tools(query="open")``).  Positional arguments (pairs without
    ``=``) are ignored since gSage proxy tools are always keyword-based.
    """
    arguments: Dict[str, Any] = {}
    for pair in _split_top_level(args_str, ","):
        pair = pair.strip()
        if not pair:
            continue
        kv = _split_top_level(pair, "=")
        if len(kv) < 2:
            continue
        key = kv[0].strip().strip("\"'")
        value = "=".join(kv[1:]).strip()
        if not key:
            continue
        arguments[key] = _coerce_value(value)
    return arguments


class _BareCallScanner:
    """Recover bare ``NAME(args)`` pythonic calls from streamed plain text.

    Some models (notably Qwen3 under large prompts) narrate the action and then
    emit the call as ordinary text with **no** markup at all, e.g.::

        Vou buscar as campanhas. search_tools(query="egoi campaign")

    This scanner watches the forwarded plain-text stream for calls to a fixed
    allow-list of *known* tool names (the gSage proxy tools), so prose that
    merely contains parentheses is never misread.  It is fragmentation-tolerant:
    a name or an unterminated argument list is held back until the rest of the
    call arrives.
    """

    def __init__(self, names: List[str]) -> None:
        self._names = list(names)
        self._buffer = ""
        # ``NAME(`` opening, with optional whitespace before the parenthesis.
        self._pattern = re.compile(
            r"\b(" + "|".join(re.escape(n) for n in self._names) + r")\s*\(",
        )
        # Holdback markers used to keep a partial ``NAME(`` prefix buffered
        # until more chunks arrive.
        self._holdback = [f"{n}(" for n in self._names]

    def feed(self, text: str) -> tuple[str, List[ToolCallData]]:
        """Consume *text*; return ``(text_to_forward, recovered_calls)``."""
        if not text:
            return "", []
        self._buffer += text
        return self._scan(final=False)

    def flush(self) -> tuple[str, List[ToolCallData]]:
        """Drain any buffered tail at end of stream."""
        return self._scan(final=True)

    def _scan(self, final: bool) -> tuple[str, List[ToolCallData]]:
        emit: List[str] = []
        calls: List[ToolCallData] = []
        while True:
            match = self._pattern.search(self._buffer)
            if match is None:
                if final:
                    emit.append(self._buffer)
                    self._buffer = ""
                else:
                    hold = _longest_partial_suffix(self._buffer, self._holdback)
                    cut = len(self._buffer) - hold
                    emit.append(self._buffer[:cut])
                    self._buffer = self._buffer[cut:]
                break
            open_idx = match.end() - 1
            inner, end = _extract_parenthesized(self._buffer, open_idx)
            if end is None:
                # Argument list not finished yet.
                emit.append(self._buffer[: match.start()])
                self._buffer = self._buffer[match.start() :]
                if final:
                    # Best-effort recovery of a truncated trailing call.
                    calls.append(
                        ToolCallData(name=match.group(1), arguments=_parse_kwargs(inner))
                    )
                    self._buffer = ""
                break
            emit.append(self._buffer[: match.start()])
            calls.append(
                ToolCallData(name=match.group(1), arguments=_parse_kwargs(inner))
            )
            self._buffer = self._buffer[end:]
        return "".join(emit), calls


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


#: Max number of characters of forwarded text kept as a debug sample.
_TEXT_SAMPLE_LIMIT = 600

#: Heuristic markers that suggest a tool call leaked as plain text but was not
#: recovered (e.g. a reasoning model narrating a JSON/pythonic call, or a DSML
#: block whose marker variant is not in the dialect's marker set yet).
_LEAK_HINT_RE = re.compile(
    r"(tool_call|run_discovered_tool|run_approved_tool|"
    r'"name"\s*:|"arguments"\s*:|"parameters"\s*:|"tool_name"\s*:|'
    r"call\s*:\s*[A-Za-z_]|"
    r"DSML|invoke\s+name\s*=|parameter\s+name\s*=)",
    re.IGNORECASE,
)


def _looks_like_leaked_tool_call(text: str) -> bool:
    """Best-effort detection of a tool call that leaked as plain text."""
    return bool(text) and bool(_LEAK_HINT_RE.search(text))


def _text_of(mr: ModelResponse) -> str:
    """Return the plain-text content of a delta, or '' when it carries none."""
    if getattr(mr, "tool_calls", None):
        return ""
    content = getattr(mr, "content", None)
    return content if isinstance(content, str) else ""


class ToolCallStreamParser:
    """Incremental parser that recovers tool calls from streamed text.

    Feed each :class:`ModelResponse` delta via :meth:`feed`; call
    :meth:`flush` once after the stream ends.  Both are generators of
    :class:`ModelResponse` objects to be re-yielded downstream.

    Resilient to:
      * markers split across multiple SSE chunks (holdback buffering);
      * multiple tool-call blocks in one stream (parallel/sequential calls);
      * incomplete trailing blocks (best-effort parse, else raw fallback).

    ``label`` tags the log lines (e.g. ``RecoveringToolCallOpenAI``) so
    diagnostics identify the provider adapter that owns the parser.
    """

    def __init__(
        self, dialect: ToolCallDialect, label: str = "ToolCallStreamParser"
    ) -> None:
        self._dialect = dialect
        self._label = label or "ToolCallStreamParser"
        self._state = _State.PASSTHROUGH
        self._buffer = ""
        self._open_marker = ""
        self._tool_index = 0
        bare_names = list(getattr(dialect, "bare_call_names", []) or [])
        self._bare: Optional[_BareCallScanner] = (
            _BareCallScanner(bare_names) if bare_names else None
        )
        self._enabled = bool(getattr(dialect, "start_markers", None)) or bool(bare_names)
        # -- DEBUG instrumentation counters (per stream/request) -----------
        self._native_tool_calls = 0      # structured tool_calls from upstream
        self._recovered_tool_calls = 0   # tool_calls rebuilt from leaked text
        self._text_chars = 0             # plain-text content forwarded as-is
        self._deltas_in = 0              # upstream deltas fed into the parser
        self._text_sample = ""           # short head of forwarded text (debug)
        self._native_tool_names: List[str] = []     # names seen in native calls
        self._recovered_tool_names: List[str] = []  # names rebuilt from text
        self._finish_reason: Optional[str] = None   # last finish_reason from provider
        self._output_tokens: Optional[int] = None   # output tokens from provider usage
        self._input_tokens: Optional[int] = None    # input tokens from provider usage

    # -- public API ------------------------------------------------------

    def feed(self, model_response: ModelResponse) -> Iterator[ModelResponse]:
        """Process one streamed delta, yielding transformed deltas."""
        self._deltas_in += 1
        # Capture provider-side diagnostics carried on any delta.
        pd = getattr(model_response, "provider_data", None)
        if isinstance(pd, dict):
            fr = pd.get("finish_reason")
            if isinstance(fr, str) and fr:
                self._finish_reason = fr
        usage = getattr(model_response, "response_usage", None)
        if usage is not None:
            ot = getattr(usage, "output_tokens", None)
            if isinstance(ot, int):
                self._output_tokens = ot
            it = getattr(usage, "input_tokens", None)
            if isinstance(it, int):
                self._input_tokens = it
        # Always pass native structured tool calls straight through.
        if model_response.tool_calls:
            self._native_tool_calls += len(model_response.tool_calls)
            for tc in model_response.tool_calls:
                fn = getattr(tc, "function", None)
                name = getattr(fn, "name", None) if fn is not None else None
                if isinstance(name, str) and name:
                    self._native_tool_names.append(name)
            yield model_response
            return

        content = model_response.content
        # Forward any non-content payload (usage, reasoning, role, …) intact.
        if not isinstance(content, str) or content == "":
            yield model_response
            return

        if not self._enabled:
            self._account_text(content)
            yield model_response
            return

        # Strip the content from the original delta but preserve its other
        # fields (token usage, reasoning_content, provider_data, …).
        if _has_non_content_payload(model_response):
            passthrough = _clone_without_content(model_response)
            yield passthrough

        self._buffer += content
        for out in self._drain():
            yield from self._post_bare(out)

    def flush(self) -> Iterator[ModelResponse]:
        """Flush any buffered text after the stream ends."""
        for out in self._marker_flush():
            yield from self._post_bare(out)
        if self._bare is not None:
            emit, calls = self._bare.flush()
            if emit:
                self._account_text(emit)
                yield ModelResponse(content=emit)
            if calls:
                yield self._emit_tool_calls(calls)

    def _marker_flush(self) -> Iterator[ModelResponse]:
        """Flush the marker state machine's buffer (text routed through bare)."""
        if not self._buffer:
            return
        if self._state == _State.IN_TOOL_CALL:
            # Try a best-effort parse of an unterminated block.
            calls = self._safe_parse(self._buffer)
            if calls:
                yield self._emit_tool_calls(calls)
            else:
                log_warning(
                    f"{self._label}: unterminated tool-call block at end "
                    f"of stream, forwarding as text ({len(self._buffer)} chars)"
                )
                # Re-emit the consumed opening marker so the original text is
                # preserved verbatim (important for legitimate code fences).
                yield ModelResponse(content=self._open_marker + self._buffer)
        else:
            # Any held-back passthrough tail is safe to emit now.
            yield ModelResponse(content=self._buffer)
        self._buffer = ""

    def _post_bare(self, out: ModelResponse) -> Iterator[ModelResponse]:
        """Route a marker-stage output through the bare-call scanner.

        Tool-call deltas (native or marker-recovered) pass straight through;
        only plain text is scanned for bare ``NAME(args)`` pythonic calls.
        """
        if self._bare is None:
            self._account_text(_text_of(out))
            yield out
            return
        text = _text_of(out)
        if not text:
            # Non-text payload (tool_calls, usage, reasoning, …) — pass through.
            yield out
            return
        emit, calls = self._bare.feed(text)
        if emit:
            self._account_text(emit)
            yield ModelResponse(content=emit)
        if calls:
            yield self._emit_tool_calls(calls)

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
                self._open_marker = marker
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
                    f"{self._label}: failed to parse tool-call block, "
                    "forwarding as text"
                )
                # Re-emit the consumed markers so legitimate (non-tool-call)
                # blocks — e.g. a plain ```json code fence — survive intact.
                yield ModelResponse(content=self._open_marker + block + marker)
            self._open_marker = ""
            continue

    def _safe_parse(self, block: str) -> List[ToolCallData]:
        try:
            return self._dialect.parse_block(block)
        except Exception as exc:  # noqa: BLE001 - best-effort, never crash the stream
            log_warning(f"{self._label}: tool-call parser error: {exc}")
            return []

    def _account_text(self, text: str) -> None:
        """Record forwarded plain text for DEBUG diagnostics."""
        if not text:
            return
        self._text_chars += len(text)
        if len(self._text_sample) < _TEXT_SAMPLE_LIMIT:
            self._text_sample += text[: _TEXT_SAMPLE_LIMIT - len(self._text_sample)]

    def _emit_tool_calls(self, calls: List[ToolCallData]) -> ModelResponse:
        deltas: List[ChoiceDeltaToolCall] = []
        for call in calls:
            deltas.append(_make_delta_tool_call(self._tool_index, call.name, call.arguments))
            self._tool_index += 1
        self._recovered_tool_calls += len(deltas)
        self._recovered_tool_names.extend(c.name for c in calls)
        log_debug(
            f"{self._label}: recovered {len(deltas)} tool call(s) from text: "
            f"{[c.name for c in calls]}"
        )
        return ModelResponse(tool_calls=deltas)  # type: ignore[arg-type]

    # -- DEBUG diagnostics ----------------------------------------------

    def log_stream_summary(self) -> None:
        """Emit a concise DEBUG summary of what the stream produced.

        Also raises a WARNING in the most actionable failure mode: the model
        produced only plain text and no tool call at all — typical of a
        reasoning/"thinking" model (e.g. Qwen3) that narrates the action
        instead of emitting it.  In that case the leaked text often still
        *looks* like a tool call (``"name":``/``run_discovered_tool``/…).
        """
        dialect_name = type(self._dialect).__name__
        suspect = _looks_like_leaked_tool_call(self._text_sample)
        log_debug(
            f"{self._label} stream summary: "
            f"dialect={dialect_name} deltas_in={self._deltas_in} "
            f"native_tool_calls={self._native_tool_calls} "
            f"recovered_tool_calls={self._recovered_tool_calls} "
            f"text_chars={self._text_chars} "
            f"native_tool_names={self._native_tool_names} "
            f"recovered_tool_names={self._recovered_tool_names} "
            f"input_tokens={self._input_tokens} "
            f"output_tokens={self._output_tokens} "
            f"finish_reason={self._finish_reason} "
            f"suspect_unrecovered_toolcall={suspect}"
        )
        total_calls = self._native_tool_calls + self._recovered_tool_calls
        if total_calls == 0 and self._text_chars > 0 and suspect:
            # Only warn in the actionable case: the text *looks* like a tool
            # call that was never emitted in structured form.  A plain prose
            # answer with no tool call is normal and stays at DEBUG level.
            log_warning(
                f"{self._label}: no structured tool call this turn, but "
                f"the forwarded text resembles one (dialect={dialect_name}, "
                f"text_chars={self._text_chars}). Likely a reasoning/thinking "
                "model narrating the action instead of emitting it — consider "
                "disabling the model's thinking mode "
                "(VLLM_ENABLE_THINKING=false or the provider equivalent). "
                f"text_head={self._text_sample[:200]!r}"
            )
        # Additional signal: model emitted only a short narration and stopped
        # without calling any tool — classic "preamble then stop" failure of a
        # reasoning model under a large prompt.  Distinct from the "looks like
        # a leaked call" case above; here the text is plain prose.
        elif (
            total_calls == 0
            and self._finish_reason == "stop"
            and self._output_tokens is not None
            and self._output_tokens < 60
            and self._text_chars > 0
        ):
            log_warning(
                f"{self._label}: model produced a short narration and "
                f"stopped without calling any tool (dialect={dialect_name}, "
                f"output_tokens={self._output_tokens}, "
                f"input_tokens={self._input_tokens}, "
                f"finish_reason={self._finish_reason}). Typical of a reasoning "
                "model refusing tool-use under a large prompt — try "
                "reducing prompt size, toggling the model's thinking mode "
                "(VLLM_ENABLE_THINKING / provider equivalent), or "
                f"setting tool_choice='required'. text_head={self._text_sample[:200]!r}"
            )


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
# Tool-call argument sanitization
# ---------------------------------------------------------------------------


def sanitize_tool_call_arguments(args_str: str) -> str:
    """Sanitize a tool-call ``function.arguments`` string that may contain
    concatenated JSON objects.

    Some reasoning models (notably Qwen) leak thinking output into the
    ``arguments`` field, producing fragments like ``{}{\"tool_name\": \"x\"}``
    — two JSON objects concatenated.  vLLM's chat template internally
    ``json.loads()`` this field for multi-turn tool-call history, and
    concatenated JSON causes the **\"Extra data: line 1 column …\"** 400
    error.

    Strategy
    --------
    1. Fast path: ``json.loads`` succeeds → return original unchanged.
    2. Scan for balanced ``{…}`` objects; return the **last** valid one
       re-serialised (the thinking leak is always an empty ``{}`` that
       appears *before* the real arguments).
    3. No valid objects found → return original string (pass-through).

    Parameters
    ----------
    args_str : str
        Raw tool-call arguments string (may be concatenated JSON).

    Returns
    -------
    str
        Sanitized JSON string, or the original if no fix was possible.
    """
    if not args_str or not isinstance(args_str, str):
        return args_str

    # Fast path: already valid JSON.
    try:
        json.loads(args_str)
        return args_str
    except json.JSONDecodeError:
        pass

    # Scan for balanced top-level ``{…}`` objects.
    objects: List[Any] = []
    i = 0
    n = len(args_str)
    while i < n:
        if args_str[i] == "{":
            inner, end = _extract_braced(args_str, i)
            candidate = "{" + (inner or "") + "}"
            try:
                objects.append(json.loads(candidate))
            except (json.JSONDecodeError, TypeError):
                pass
            i = end
        else:
            i += 1

    if objects:
        # Return the LAST valid object — thinking output (``{}``) comes first.
        last = objects[-1]
        try:
            sanitized = json.dumps(last, ensure_ascii=False)
            log_debug(
                "sanitize_tool_call_arguments: extracted last valid JSON "
                f"object from concatenated arguments "
                f"(original_len={len(args_str)} sanitized_len={len(sanitized)})"
            )
            return sanitized
        except (TypeError, ValueError):
            pass

    return args_str
