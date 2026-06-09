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
  It additionally recovers **bare** ``NAME(args)`` pythonic calls (no markup
  at all) for a fixed allow-list of gSage proxy tools — the shape a reasoning
  model (e.g. Qwen3) tends to leak when it narrates the action under a large
  prompt instead of emitting a structured tool call.
* :class:`ToolCallDialect` — a pluggable contract so additional open-source
  tool-calling dialects (Hermes, Llama, …) can be added later.
* :class:`RecoveringToolCallVLLM` — an Agno :class:`~agno.models.vllm.VLLM`
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
import logging
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
    "QwenHermesDialect",
    "NoOpDialect",
    "ToolCallStreamParser",
    "RecoveringToolCallVLLM",
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


#: gSage proxy tool names.  These are the only functions a model can legitimately
#: call in the proxy-tools pattern, so they are the only names we attempt to
#: recover from *bare* (marker-less) pythonic calls — keeping false positives on
#: ordinary prose containing parentheses effectively impossible.
_GSAGE_PROXY_TOOL_NAMES = ("search_tools", "run_discovered_tool", "run_approved_tool")


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
    #: Bare ``NAME(args)`` calls recovered even without surrounding markers.
    bare_call_names = list(_GSAGE_PROXY_TOOL_NAMES)

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


# ---------------------------------------------------------------------------
# Qwen / Hermes JSON dialect
# ---------------------------------------------------------------------------


class QwenHermesDialect:
    """Dialect for Qwen 2.5/3 tool calls leaked by the vLLM ``hermes`` parser.

    Qwen models (and other Hermes-style models such as NousResearch Hermes)
    encode each tool call as a JSON object wrapped in ``<tool_call>`` tags::

        <tool_call>
        {"name": "glpi_search", "arguments": {"query": "open tickets"}}
        </tool_call>

    When vLLM runs **without** a tool-call parser (or fails to flush the
    streaming buffer) the model improvises and the call escapes to the client
    as plain text — sometimes inside ``<tool_call>`` tags, sometimes inside a
    bare Markdown ```` ```json ```` fence, e.g.::

        ```json
        {"tool_name": "run_discovered_tool", "params": {...}}
        ```

    This dialect recovers both shapes.  Tolerant to common variations:

    * tags ``<tool_call>...</tool_call>`` **or** a ```` ```json ```` fence;
    * name key ``name``/``function``/``tool_name``;
    * argument key ``arguments``/``parameters``/``params``;
    * ``arguments`` provided as a nested object **or** a JSON-encoded string;
    * several whitespace-separated JSON objects inside a single block.

    A bare ```` ```json ```` block is only converted when its JSON object
    actually looks like a tool call (has a name key *and* an arguments key);
    otherwise the original fenced text is forwarded untouched, so legitimate
    JSON code blocks are never corrupted.
    """

    start_markers = ["<tool_call>", "```json", "```tool_call"]
    end_markers = ["</tool_call>", "```"]
    #: Bare ``NAME(args)`` calls recovered even without surrounding markers.
    bare_call_names = list(_GSAGE_PROXY_TOOL_NAMES)

    #: Strips an optional ```json ... ``` Markdown fence wrapping the body.
    _FENCE_RE = re.compile(r"^```(?:json|tool_call)?\s*|\s*```$", re.IGNORECASE)

    def parse_block(self, body: str) -> List[ToolCallData]:
        text = self._FENCE_RE.sub("", body.strip()).strip()
        if not text:
            return []
        calls: List[ToolCallData] = []
        for obj in _iter_json_objects(text):
            call = _tool_call_from_hermes_obj(obj)
            if call is not None:
                calls.append(call)
        return calls


def _tool_call_from_hermes_obj(obj: Any) -> Optional[ToolCallData]:
    """Build a :class:`ToolCallData` from a parsed Hermes/Qwen JSON object.

    Returns ``None`` when the object does not look like a tool call (so plain
    JSON code blocks are forwarded as text instead of being swallowed).
    """
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("function") or obj.get("tool_name")
    if not isinstance(name, str) or not name:
        return None
    # Require an explicit arguments key so arbitrary JSON objects that merely
    # happen to contain a "name" field are not misread as tool calls.
    arg_keys = ("arguments", "parameters", "params", "args")
    if not any(k in obj for k in arg_keys):
        return None
    raw_args: Any = None
    for k in arg_keys:
        if k in obj:
            raw_args = obj[k]
            break
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except (ValueError, TypeError):
            raw_args = {}
    if not isinstance(raw_args, dict):
        raw_args = {}
    return ToolCallData(name=name, arguments=raw_args)


def _iter_json_objects(text: str) -> List[Any]:
    """Parse one or more JSON objects from *text* (best-effort).

    First tries a single ``json.loads`` (the common case).  If that fails,
    scans for balanced top-level ``{...}`` objects and parses each one,
    tolerating whitespace/newlines between concatenated tool calls.
    """
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else [parsed]
    except (ValueError, TypeError):
        pass
    objects: List[Any] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "{":
            inner, end = _extract_braced(text, i)
            candidate = "{" + (inner or "") + "}"
            try:
                objects.append(json.loads(candidate))
            except (ValueError, TypeError):
                pass
            i = end
        else:
            i += 1
    return objects


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
    """Resolve a dialect by name.

    Supported names:

    * ``"gemma"`` (aliases: ``gemma4``, ``pythonic``) — Gemma pythonic markup.
    * ``"qwen"`` (aliases: ``hermes``, ``qwen3``, ``json``) — Hermes/Qwen JSON
      ``<tool_call>{...}</tool_call>`` markup.
    * ``None``/``"none"`` — passthrough (native ``tool_calls`` only).
    """
    if name is None:
        return NoOpDialect()
    key = name.strip().lower()
    if key in ("", "none", "off", "disabled"):
        return NoOpDialect()
    if key in ("gemma", "gemma4", "gemma_pythonic", "pythonic"):
        return GemmaPythonicDialect()
    if key in ("qwen", "qwen2", "qwen3", "hermes", "json"):
        return QwenHermesDialect()
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

    Mirrors :func:`_parse_pythonic_args` but for the ``=`` keyword-argument
    syntax used by bare pythonic calls (``search_tools(query="open")``).
    Positional arguments (pairs without ``=``) are ignored since gSage proxy
    tools are always keyword-based.
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
#: recovered (e.g. a reasoning model narrating a JSON/pythonic call).
_LEAK_HINT_RE = re.compile(
    r"(tool_call|run_discovered_tool|run_approved_tool|"
    r'"name"\s*:|"arguments"\s*:|"parameters"\s*:|"tool_name"\s*:|'
    r"call\s*:\s*[A-Za-z_])",
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


# ---------------------------------------------------------------------------
# DEBUG request/response introspection helpers
# ---------------------------------------------------------------------------

#: Agno's logger; used to skip building debug snapshots unless DEBUG is active.
_AGNO_LOGGER = logging.getLogger("agno")


def _debug_enabled() -> bool:
    """True when DEBUG logging is active (so we can skip snapshot building)."""
    return _AGNO_LOGGER.isEnabledFor(logging.DEBUG)


def _message_content_len(content: Any) -> int:
    """Approximate character length of a message ``content`` (str or parts)."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    total += len(text)
        return total
    return 0


def _summarize_messages(messages: Any) -> Dict[str, Any]:
    """Compact, content-free summary of the outgoing message list.

    Reports the message count, a per-role breakdown, the approximate total
    content size (chars) and how many tool-call references the history carries
    — never the message bodies themselves.
    """
    if not isinstance(messages, (list, tuple)):
        return {"count": 0}
    roles: Dict[str, int] = {}
    total_chars = 0
    history_tool_calls = 0
    for m in messages:
        if isinstance(m, dict):
            role = str(m.get("role") or "?")
            content = m.get("content")
            tcs = m.get("tool_calls")
        else:
            role = str(getattr(m, "role", None) or "?")
            content = getattr(m, "content", None)
            tcs = getattr(m, "tool_calls", None)
        roles[role] = roles.get(role, 0) + 1
        total_chars += _message_content_len(content)
        if isinstance(tcs, (list, tuple)):
            history_tool_calls += len(tcs)
    return {
        "count": len(messages),
        "roles": roles,
        "content_chars": total_chars,
        "history_tool_calls": history_tool_calls,
    }


def _summarize_tools(tools: Any) -> Dict[str, Any]:
    """Compact summary of the tool schemas advertised to the model.

    Reports tool count, names, serialized size (chars ≈ 4 × tokens), and the
    top-5 heaviest schemas — the dominant token cost in any tool-use request.
    """
    if not isinstance(tools, (list, tuple)):
        return {"count": 0, "names": [], "serialized_chars": 0}
    names: List[str] = []
    sizes: List[tuple[str, int]] = []
    total = 0
    for t in tools:
        if isinstance(t, dict):
            fn = t.get("function") if isinstance(t.get("function"), dict) else {}
            name = (fn or {}).get("name") or t.get("name")
            if isinstance(name, str) and name:
                names.append(name)
            try:
                size = len(json.dumps(t, ensure_ascii=False, default=str))
            except Exception:  # noqa: BLE001
                size = -1
            total += max(size, 0)
            if isinstance(name, str) and name and size >= 0:
                sizes.append((name, size))
    top5 = sorted(sizes, key=lambda p: -p[1])[:5]
    return {
        "count": len(tools),
        "names": names,
        "serialized_chars": total,
        "top5_by_size": top5,
    }


def _extract_invoke_arg(
    name: str, position: int, args: tuple, kwargs: Dict[str, Any]
) -> Any:
    """Resolve an invoke argument by keyword first, then positional fallback."""
    if name in kwargs:
        return kwargs[name]
    if len(args) > position:
        return args[position]
    return None


def _get_request_dump_dir() -> Optional[str]:
    """Return the configured request-dump directory, or ``None`` when unset.

    The directory is created on first use; failures are swallowed so a
    misconfigured path never breaks the request path.  Resolved per-call so
    the setting can be toggled live without restarting the process.
    """
    try:
        from src.shared.config.settings import get_settings

        path = (get_settings().vllm_debug_request_dump_path or "").strip()
    except Exception:  # noqa: BLE001 - keep request path tolerant
        return None
    if not path:
        return None
    try:
        import os

        os.makedirs(path, exist_ok=True)
    except Exception:  # noqa: BLE001
        return None
    return path


def _dump_request_payload(
    dump_dir: str,
    *,
    model_id: str,
    messages: Any,
    tools: Any,
    tool_choice: Any,
    extra_body: Any,
    enable_thinking: Any,
) -> None:
    """Write the outgoing request to a timestamped JSON file.

    Opt-in via ``VLLM_DEBUG_REQUEST_DUMP_PATH``.  Used to bit-for-bit replay
    a production request in ``limbo/debug_vllm_toolcalls.py --replay``.
    Includes full message bodies (prompts + history) — dev/diagnostic only,
    do NOT enable in production with real PII.
    """
    import os
    import time as _time

    def _to_jsonable(obj: Any) -> Any:
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj
        if isinstance(obj, dict):
            return {str(k): _to_jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_to_jsonable(v) for v in obj]
        for attr in ("model_dump", "dict", "to_dict"):
            fn = getattr(obj, attr, None)
            if callable(fn):
                try:
                    return _to_jsonable(fn())
                except Exception:  # noqa: BLE001
                    pass
        return repr(obj)

    payload = {
        "captured_at": _time.time(),
        "model_id": model_id,
        "enable_thinking": enable_thinking,
        "messages": _to_jsonable(messages),
        "tools": _to_jsonable(tools),
        "tool_choice": _to_jsonable(tool_choice),
        "extra_body": _to_jsonable(extra_body),
    }
    fname = f"req_{int(_time.time() * 1000)}_{uuid4().hex[:8]}.json"
    try:
        with open(os.path.join(dump_dir, fname), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        log_warning(f"RecoveringToolCallVLLM: request dump failed: {exc}")


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
                    "vLLM tool-call parser: unterminated tool-call block at end "
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
                    "vLLM tool-call parser: failed to parse tool-call block, "
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
            log_warning(f"vLLM tool-call parser error: {exc}")
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
            f"vLLM tool-call parser recovered {len(deltas)} tool call(s) from text: "
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
            "RecoveringToolCallVLLM stream summary: "
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
                "RecoveringToolCallVLLM: no structured tool call this turn, but "
                f"the forwarded text resembles one (dialect={dialect_name}, "
                f"text_chars={self._text_chars}). Likely a reasoning/thinking "
                "model narrating the action instead of emitting it — consider "
                f"VLLM_ENABLE_THINKING=false. text_head={self._text_sample[:200]!r}"
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
                "RecoveringToolCallVLLM: model produced a short narration and "
                f"stopped without calling any tool (dialect={dialect_name}, "
                f"output_tokens={self._output_tokens}, "
                f"input_tokens={self._input_tokens}, "
                f"finish_reason={self._finish_reason}). Typical of a reasoning "
                "model refusing tool-use under a large prompt — try "
                "reducing prompt size, toggling VLLM_ENABLE_THINKING, or "
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
# Agno model subclass
# ---------------------------------------------------------------------------


@dataclass
class RecoveringToolCallVLLM(VLLM):
    """vLLM model that recovers text-leaked tool calls during streaming.

    Works for any open-source model whose tool calls leak as plain text
    (Gemma pythonic, Qwen/Hermes JSON, …); the concrete text shape is selected
    via ``tool_call_dialect``.

    Parameters
    ----------
    tool_call_dialect:
        Name of the text dialect to detect (``"gemma"``, ``"qwen"`` or
        ``None`` to disable text parsing while keeping native passthrough).
    force_non_streaming:
        When ``True``, streaming requests are internally served by a single
        non-streaming call (where vLLM emits correct ``tool_calls``) and then
        replayed as one delta.  Use as a robustness fallback when the parser
        is not sufficient.
    """

    name: str = "RecoveringToolCallVLLM"
    provider: str = "VLLM"

    tool_call_dialect: Optional[str] = "gemma"
    force_non_streaming: bool = False

    def _new_parser(self) -> ToolCallStreamParser:
        return ToolCallStreamParser(build_dialect(self.tool_call_dialect))

    def _parse_provider_response_delta(self, response_delta: Any) -> ModelResponse:
        """Same as the upstream parser, plus surface ``finish_reason``.

        Agno's :class:`~agno.models.openai.chat.OpenAIChat` drops the per-chunk
        ``finish_reason`` when mapping to :class:`ModelResponse`.  We need it
        for diagnostics (it tells us whether the model stopped naturally,
        switched to tool_calls, hit a token cap, …) so we copy it into the
        existing ``provider_data`` payload.
        """
        model_response = super()._parse_provider_response_delta(response_delta)
        try:
            choices = getattr(response_delta, "choices", None)
            if choices:
                fr = getattr(choices[0], "finish_reason", None)
                if fr:
                    if model_response.provider_data is None:
                        model_response.provider_data = {}
                    model_response.provider_data["finish_reason"] = fr
        except Exception:  # noqa: BLE001 - best-effort instrumentation only
            pass
        return model_response

    def _log_request_snapshot(self, args: tuple, kwargs: Dict[str, Any]) -> None:
        """Emit a compact DEBUG snapshot of the request sent to vLLM.

        Logs only the *structure* — message-count/roles/size and the advertised
        tool names — never the message bodies, so prompts and PII stay out of
        the logs.  Skipped entirely unless DEBUG logging is active.

        When the ``VLLM_DEBUG_REQUEST_DUMP_PATH`` setting points to a
        writable directory, the full request payload (messages + tools +
        tool_choice + extra_body) is dumped to a timestamped JSON file so a
        bisection harness can replay the exact production request.
        """
        debug_on = _debug_enabled()
        dump_dir = _get_request_dump_dir()
        if not debug_on and not dump_dir:
            return
        messages = _extract_invoke_arg("messages", 0, args, kwargs)
        tools = _extract_invoke_arg("tools", 3, args, kwargs)
        tool_choice = _extract_invoke_arg("tool_choice", 4, args, kwargs)
        if debug_on:
            request_params = getattr(self, "request_params", None) or {}
            extra_body = request_params.get("extra_body") if isinstance(request_params, dict) else None
            extra_body_keys = sorted(extra_body.keys()) if isinstance(extra_body, dict) else None
            log_debug(
                "RecoveringToolCallVLLM request -> "
                f"model={self.id} dialect={self.tool_call_dialect} "
                f"streaming={not self.force_non_streaming} "
                f"enable_thinking={getattr(self, 'enable_thinking', None)} "
                f"messages={_summarize_messages(messages)} "
                f"tools={_summarize_tools(tools)} "
                f"tool_choice={tool_choice} "
                f"response_format={'set' if isinstance(request_params, dict) and request_params.get('response_format') else None} "
                f"stream_options={(request_params or {}).get('stream_options') if isinstance(request_params, dict) else None} "
                f"extra_body_keys={extra_body_keys}"
            )
        if dump_dir:
            _dump_request_payload(
                dump_dir,
                model_id=self.id,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                extra_body=(
                    (getattr(self, "request_params", None) or {}).get("extra_body")
                    if isinstance(getattr(self, "request_params", None), dict)
                    else None
                ),
                enable_thinking=getattr(self, "enable_thinking", None),
            )

    def _normalize_nonstream_response(self, model_response: ModelResponse) -> ModelResponse:
        """Convert a non-streaming ModelResponse into streaming-shaped deltas."""
        if model_response.tool_calls:
            model_response.tool_calls = _dict_tool_calls_to_deltas(  # type: ignore[assignment]
                list(model_response.tool_calls)
            )
        return model_response

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        parser = self._new_parser()
        self._log_request_snapshot(args, kwargs)
        if self.force_non_streaming:
            model_response = self.invoke(*args, **kwargs)
            yield from parser.feed(self._normalize_nonstream_response(model_response))
            yield from parser.flush()
            parser.log_stream_summary()
            return
        for delta in super().invoke_stream(*args, **kwargs):
            yield from parser.feed(delta)
        yield from parser.flush()
        parser.log_stream_summary()

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        parser = self._new_parser()
        self._log_request_snapshot(args, kwargs)
        if self.force_non_streaming:
            model_response = await self.ainvoke(*args, **kwargs)
            for out in parser.feed(self._normalize_nonstream_response(model_response)):
                yield out
            for out in parser.flush():
                yield out
            parser.log_stream_summary()
            return
        async for delta in super().ainvoke_stream(*args, **kwargs):
            for out in parser.feed(delta):
                yield out
        for out in parser.flush():
            yield out
        parser.log_stream_summary()
