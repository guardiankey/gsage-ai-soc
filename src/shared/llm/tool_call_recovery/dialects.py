"""Tool-call text dialects used to recover calls leaked as plain text.

A dialect describes how one model family encodes tool calls inside ordinary
text (markers + body grammar).  The stream parser in
:mod:`src.shared.llm.tool_call_recovery.parser` consumes dialects to convert
leaked blocks back into structured tool calls.

Supported dialects:

* ``"gemma"`` — Gemma 4 pythonic markup (``<|tool_call>call:NAME{...}``).
* ``"qwen"`` — Qwen/Hermes JSON markup (``<tool_call>{...}</tool_call>`` or a
  `````json` fence).
* ``"dsml"`` — DeepSeek DSML markup (``<｜DSML｜ calls>`` wrapper with
  ``<｜DSML｜ invoke name="...">`` / ``<｜DSML｜ parameter ...>`` children),
  as leaked by Azure AI Foundry OpenAI-compatible endpoints.
* ``"none"`` — passthrough (native ``tool_calls`` only).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from agno.utils.log import log_warning

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
    inside plain text so the stream parser can detect and decode them
    incrementally.
    """

    #: Marker strings that open a tool-call block (any may appear).
    start_markers: List[str]
    #: Marker strings that close a tool-call block (any may appear).
    end_markers: List[str]

    def parse_block(self, body: str) -> List[ToolCallData]:
        """Parse the text *between* start and end markers into tool calls."""
        ...


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


# ---------------------------------------------------------------------------
# DeepSeek / DSML dialect (Azure AI Foundry OpenAI-compatible endpoints)
# ---------------------------------------------------------------------------

#: Words accepted for the DSML outer wrapper that opens/closes a tool-call block.
_DSML_WRAPPER_WORDS = ("calls", "tool_calls")

#: Vertical-bar spellings observed in DSML special tokens: fullwidth U+FF5C
#: (DeepSeek tokenizer style) and the ASCII fallback some deployments emit.
_DSML_BARS = ("\uff5c", "|")


def _dsml_wrapper_markers(closing: bool) -> List[str]:
    """Build the accepted DSML outer-wrapper marker variants.

    The DSML special-token spelling varies across model builds/deployments
    (fullwidth vs ASCII bar, optional second bar, optional space/underscore
    separator, optional trailing bar), so instead of a single literal marker
    we accept a bounded, deterministic variant set.  Extend this generator
    (plus a unit-test fixture) when a new production variant is observed.
    """
    lead = "</" if closing else "<"
    markers: List[str] = []
    for bar in _DSML_BARS:
        for second in ("", bar):
            for gap in ("", " ", "\u2581"):
                for word in _DSML_WRAPPER_WORDS:
                    markers.append(f"{lead}{bar}DSML{second}{gap}{word}>")
                    markers.append(f"{lead}{bar}DSML{second}{gap}{word}{bar}>")
    return markers


#: Matches any DSML tag token inside a block body (open or close, any variant).
_DSML_TAG_RE = re.compile(
    r"<(?P<close>/?)\s*[|｜]?\s*DSML[^<>]*?"
    r"(?P<word>calls|tool_calls|invoke|parameter)\b[^<>]*>",
    re.IGNORECASE,
)

_DSML_NAME_ATTR_RE = re.compile(r"name\s*=\s*\"([^\"]*)\"", re.IGNORECASE)
_DSML_STRING_ATTR_RE = re.compile(r"string\s*=\s*\"([^\"]*)\"", re.IGNORECASE)
_DSML_JSON_STRING_VALUES = {"json", "false", "0"}


def _dsml_attr(raw_tag: str, attr_re: re.Pattern[str]) -> Optional[str]:
    """Return the first capture of *attr_re* inside *raw_tag*, or ``None``."""
    match = attr_re.search(raw_tag)
    return match.group(1) if match else None


def _dsml_coerce_value(raw: str, is_string: bool) -> Any:
    """Coerce a DSML ``parameter`` value.

    ``string="true"`` (the default) keeps the value as raw text; any other
    spelling tries a JSON parse first (numbers, booleans, objects) and falls
    back to the raw string.
    """
    if is_string:
        return raw
    stripped = raw.strip()
    if not stripped:
        return ""
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return stripped


class DsmlDialect:
    """Dialect for DeepSeek DSML tool calls leaked as plain text.

    Azure AI Foundry / OpenAI-compatible endpoints serving DeepSeek models
    can fail to convert the model's native tool-call grammar to OpenAI
    ``tool_calls`` during streaming, forwarding it inside ``content``::

        <｜DSML｜ calls>
        <｜DSML｜ invoke name="search_tools">
        <｜DSML｜ parameter name="query" string="true">analyze attached CSV</｜DSML｜ parameter>
        </｜DSML｜ invoke>
        <｜DSML｜ invoke name="instruction_catalog">
        <｜DSML｜ parameter name="action" string="true">list</｜DSML｜ parameter>
        </｜DSML｜ invoke>
        </｜DSML｜ calls>

    The dialect keys off the outer ``calls`` / ``tool_calls`` wrapper (marker
    variants generated by :func:`_dsml_wrapper_markers`) and parses any number
    of ``invoke`` / ``parameter`` children inside, tolerating missing close
    tags and fullwidth/ASCII bar spellings.

    Limitations
    -----------
    * Blocks **without** the outer wrapper (bare ``invoke`` tags) are not
      detected; the parser forwards the text and the suspect-leak warning in
      :func:`~...parser.log_stream_summary` still flags it so operators can
      extend the marker set.
    * An unterminated block is recovered best-effort when the stream ends.
    """

    start_markers = _dsml_wrapper_markers(closing=False)
    end_markers = _dsml_wrapper_markers(closing=True)
    #: Bare ``NAME(args)`` calls recovered even without surrounding markers.
    bare_call_names = list(_GSAGE_PROXY_TOOL_NAMES)

    def parse_block(self, body: str) -> List[ToolCallData]:
        calls: List[ToolCallData] = []
        current_name: Optional[str] = None
        current_args: Dict[str, Any] = {}
        param_name: Optional[str] = None
        param_value_start: Optional[int] = None
        param_is_string = True

        def _flush_param(end: int) -> None:
            nonlocal param_name, param_value_start
            if (
                current_name is not None
                and param_name is not None
                and param_value_start is not None
            ):
                raw = body[param_value_start:end].strip()
                current_args[param_name] = _dsml_coerce_value(raw, param_is_string)
            param_name = None
            param_value_start = None

        def _flush_call() -> None:
            nonlocal current_name, current_args
            if current_name:
                calls.append(
                    ToolCallData(name=current_name, arguments=dict(current_args))
                )
            current_name = None
            current_args = {}

        for tag in _DSML_TAG_RE.finditer(body):
            # Any tag closes an open parameter value (its text ends here).
            if param_value_start is not None:
                _flush_param(tag.start())
            raw_tag = tag.group(0)
            is_close = bool(tag.group("close")) or raw_tag.startswith("</")
            word = (tag.group("word") or "").lower()
            if word in ("calls", "tool_calls"):
                continue
            if word == "invoke":
                # Tolerate a missing close tag: a new invoke flushes the
                # previous call before starting the next one.
                _flush_call()
                if not is_close:
                    current_name = _dsml_attr(raw_tag, _DSML_NAME_ATTR_RE)
            elif word == "parameter":
                if not is_close:
                    param_name = _dsml_attr(raw_tag, _DSML_NAME_ATTR_RE)
                    string_attr = _dsml_attr(raw_tag, _DSML_STRING_ATTR_RE)
                    param_is_string = (
                        (string_attr or "true").strip().lower()
                        not in _DSML_JSON_STRING_VALUES
                    )
                    param_value_start = tag.end()

        if param_value_start is not None:
            _flush_param(len(body))
        _flush_call()
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
    """Resolve a dialect by name.

    Supported names:

    * ``"gemma"`` (aliases: ``gemma4``, ``pythonic``) — Gemma pythonic markup.
    * ``"qwen"`` (aliases: ``hermes``, ``qwen3``, ``json``) — Hermes/Qwen JSON
      ``<tool_call>{...}</tool_call>`` markup.
    * ``"dsml"`` (alias: ``deepseek``) — DeepSeek DSML markup
      (``<｜DSML｜ calls>`` / ``<｜DSML｜ invoke name="...">``).
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
    if key in ("dsml", "deepseek"):
        return DsmlDialect()
    log_warning(f"Unknown tool-call dialect '{name}', falling back to passthrough")
    return NoOpDialect()
