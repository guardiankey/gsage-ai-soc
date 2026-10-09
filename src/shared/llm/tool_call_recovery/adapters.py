"""Provider adapters that wire the tool-call recovery parser into Agno models.

:class:`_RecoveringToolCallMixin` hosts the provider-agnostic behaviour shared
by every adapter (stream parsing, request/response diagnostics, tool-call
argument sanitization).  Concrete adapters only bind it to a model class and
provide their own configuration fields:

* :class:`RecoveringToolCallVLLM` — self-hosted vLLM (keeps the existing
  ``force_non_streaming`` fallback flag).
* :class:`RecoveringToolCallOpenAI` — OpenAI-compatible endpoints (notably
  Azure AI Foundry serving DeepSeek models).  Always streaming: the recovery
  parser runs incrementally over the streamed deltas.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional
from uuid import uuid4

from agno.models.openai import OpenAIChat
from agno.models.response import ModelResponse
from agno.models.vllm import VLLM
from agno.utils.log import log_debug, log_warning

from src.shared.llm.tool_call_recovery.dialects import build_dialect
from src.shared.llm.tool_call_recovery.parser import (
    ToolCallStreamParser,
    _dict_tool_calls_to_deltas,
    sanitize_tool_call_arguments,
)


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


def _resolve_request_dump_dir(path: Optional[str]) -> Optional[str]:
    """Return the configured request-dump directory, or ``None`` when unset.

    The directory is created on first use; failures are swallowed so a
    misconfigured path never breaks the request path.  Resolved per-call so
    the setting can be toggled live without restarting the process.
    """
    candidate = (path or "").strip()
    if not candidate:
        return None
    try:
        os.makedirs(candidate, exist_ok=True)
    except Exception:  # noqa: BLE001
        return None
    return candidate


def _dump_request_payload(
    dump_dir: str,
    *,
    model_id: str,
    messages: Any,
    tools: Any,
    tool_choice: Any,
    extra_body: Any,
    enable_thinking: Any,
    final_request_params: Optional[Dict[str, Any]] = None,
    label: str = "ToolCallStreamParser",
) -> None:
    """Write the outgoing request to a timestamped JSON file.

    Opt-in via the provider's ``*_DEBUG_REQUEST_DUMP_PATH`` setting.  Used to
    bit-for-bit replay a production request in
    ``scripts/debug_vllm_toolcalls.py --replay``.  Includes full message
    bodies (prompts + history) — dev/diagnostic only, do NOT enable in
    production with real PII.
    """
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
        # Full kwargs dict that goes to OpenAI ``chat.completions.create``
        # (post Agno's ``get_request_params`` merge — includes temperature,
        # top_p, max_tokens, seed, response_format, service_tier, …).
        # ``messages``/``tools`` are NOT re-included here to keep the file
        # small; only the *extra* keys Agno appends are captured.
        "final_request_extras": _to_jsonable(
            {k: v for k, v in (final_request_params or {}).items()
             if k not in {"messages", "tools", "tool_choice", "extra_body"}}
        ),
    }
    fname = f"req_{int(_time.time() * 1000)}_{uuid4().hex[:8]}.json"
    try:
        with open(os.path.join(dump_dir, fname), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        log_warning(f"{label}: request dump failed: {exc}")


# ---------------------------------------------------------------------------
# Shared recovery mixin
# ---------------------------------------------------------------------------


class _RecoveringToolCallMixin:
    """Provider-agnostic streaming tool-call recovery.

    Must be listed as the **first** base class so its overrides win over the
    model class methods while ``super()`` calls still reach the real model
    implementation (``VLLM`` / ``OpenAIChat``).  Concrete adapters provide:

    * ``tool_call_dialect`` — dataclass field with the dialect name;
    * ``_request_dump_dir()`` — per-provider debug-dump directory hook;
    * optional ``force_non_streaming`` handling (vLLM only).

    The attribute declarations below are *annotations only* (no values) so
    they never shadow the model class attributes at runtime — they exist just
    to make the shared methods type-checkable.
    """

    name: str
    id: str
    tool_call_dialect: Optional[str]
    get_request_params: Callable[..., Dict[str, Any]]

    # -- hooks -------------------------------------------------------------

    def _request_dump_dir(self) -> Optional[str]:
        """Return the request-dump directory for this provider, or ``None``."""
        return None

    # -- shared machinery ----------------------------------------------------

    def _new_parser(self) -> ToolCallStreamParser:
        return ToolCallStreamParser(
            build_dialect(self.tool_call_dialect), label=self.name
        )

    def _format_message(
        self, message: Any, compress_tool_results: bool = False
    ) -> Dict[str, Any]:
        """Format a message, sanitizing malformed tool-call arguments.

        Calls the parent implementation, then scans ``tool_calls`` for
        ``function.arguments`` that may contain concatenated JSON (a known
        Qwen quirk) and sanitizes them via :func:`sanitize_tool_call_arguments`.
        """
        message_dict = super()._format_message(message, compress_tool_results)  # type: ignore[attr-defined]

        # Sanitize tool-call arguments that may be concatenated JSON
        # (e.g. {} followed by the real arguments — a Qwen thinking leak).
        tool_calls = message_dict.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    continue
                raw_args = fn.get("arguments")
                if isinstance(raw_args, str):
                    sanitized = sanitize_tool_call_arguments(raw_args)
                    if sanitized != raw_args:
                        fn["arguments"] = sanitized
                        # Emit a one-time warning per sanitized tool call
                        # so operators can take preventive action (thinking
                        # mode leaks into the arguments field).
                        tool_name = fn.get("name", "?")
                        log_warning(
                            f"{self.name}: sanitized concatenated JSON in "
                            f"tool-call arguments for '{tool_name}'. "
                            "The model leaked thinking output into the "
                            "arguments field — consider disabling the "
                            "model's thinking mode if available. "
                            f"(original_len={len(raw_args)} "
                            f"sanitized_len={len(sanitized)})"
                        )

        return message_dict

    def _parse_provider_response_delta(self, response_delta: Any) -> ModelResponse:
        """Same as the upstream parser, plus surface ``finish_reason``.

        Agno's :class:`~agno.models.openai.chat.OpenAIChat` drops the per-chunk
        ``finish_reason`` when mapping to :class:`ModelResponse`.  We need it
        for diagnostics (it tells us whether the model stopped naturally,
        switched to tool_calls, hit a token cap, …) so we copy it into the
        existing ``provider_data`` payload.
        """
        model_response = super()._parse_provider_response_delta(response_delta)  # type: ignore[attr-defined]
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
        """Emit a compact DEBUG snapshot of the request sent to the model.

        Logs only the *structure* — message-count/roles/size and the advertised
        tool names — never the message bodies, so prompts and PII stay out of
        the logs.  Skipped entirely unless DEBUG logging is active.

        When the provider's ``*_DEBUG_REQUEST_DUMP_PATH`` setting points to a
        writable directory, the full request payload (messages + tools +
        tool_choice + extra_body) is dumped to a timestamped JSON file so a
        bisection harness can replay the exact production request.
        """
        debug_on = _debug_enabled()
        dump_dir = self._request_dump_dir()
        if not debug_on and not dump_dir:
            return
        messages = _extract_invoke_arg("messages", 0, args, kwargs)
        tools = _extract_invoke_arg("tools", 3, args, kwargs)
        tool_choice = _extract_invoke_arg("tool_choice", 4, args, kwargs)
        response_format = _extract_invoke_arg("response_format", 2, args, kwargs)

        # Resolve the FINAL kwargs dict Agno will send to OpenAI SDK so we
        # capture every parameter the model sees (temperature, top_p,
        # max_tokens, seed, response_format, service_tier, …) — not just the
        # ones explicitly passed to invoke_stream.  Best-effort: any error
        # falls back to an empty dict so instrumentation never breaks the
        # request path.
        final_request_params: Dict[str, Any] = {}
        try:
            final_request_params = self.get_request_params(
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
            ) or {}
        except Exception as exc:  # noqa: BLE001
            log_warning(f"{self.name}: get_request_params snapshot failed: {exc}")

        if debug_on:
            extra_body = final_request_params.get("extra_body")
            extra_body_keys = sorted(extra_body.keys()) if isinstance(extra_body, dict) else None
            extras = {
                k: v for k, v in final_request_params.items()
                if k not in {"messages", "tools", "tool_choice", "extra_body"}
            }
            log_debug(
                f"{self.name} request -> "
                f"model={self.id} dialect={self.tool_call_dialect} "
                f"streaming={not getattr(self, 'force_non_streaming', False)} "
                f"enable_thinking={getattr(self, 'enable_thinking', None)} "
                f"messages={_summarize_messages(messages)} "
                f"tools={_summarize_tools(tools)} "
                f"tool_choice={tool_choice} "
                f"extra_body_keys={extra_body_keys} "
                f"final_request_extras={extras}"
            )
        if dump_dir:
            _dump_request_payload(
                dump_dir,
                model_id=self.id,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                extra_body=final_request_params.get("extra_body"),
                enable_thinking=getattr(self, "enable_thinking", None),
                final_request_params=final_request_params,
                label=self.name,
            )

    # -- streaming wrappers (always streaming) --------------------------------

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        parser = self._new_parser()
        self._log_request_snapshot(args, kwargs)
        for delta in super().invoke_stream(*args, **kwargs):  # type: ignore[attr-defined]
            yield from parser.feed(delta)
        yield from parser.flush()
        parser.log_stream_summary()

    async def ainvoke_stream(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        parser = self._new_parser()
        self._log_request_snapshot(args, kwargs)
        async for delta in super().ainvoke_stream(*args, **kwargs):  # type: ignore[attr-defined]
            for out in parser.feed(delta):
                yield out
        for out in parser.flush():
            yield out
        parser.log_stream_summary()


# ---------------------------------------------------------------------------
# Concrete adapters
# ---------------------------------------------------------------------------


@dataclass
class RecoveringToolCallVLLM(_RecoveringToolCallMixin, VLLM):
    """vLLM model that recovers text-leaked tool calls during streaming.

    Works for any open-source model whose tool calls leak as plain text
    (Gemma pythonic, Qwen/Hermes JSON, DeepSeek DSML, …); the concrete text
    shape is selected via ``tool_call_dialect``.

    Parameters
    ----------
    tool_call_dialect:
        Name of the text dialect to detect (``"gemma"``, ``"qwen"``,
        ``"dsml"`` or ``None`` to disable text parsing while keeping native
        passthrough).
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

    def _request_dump_dir(self) -> Optional[str]:
        try:
            from src.shared.config.settings import get_settings

            return _resolve_request_dump_dir(get_settings().vllm_debug_request_dump_path)
        except Exception:  # noqa: BLE001 - keep request path tolerant
            return None

    def _normalize_nonstream_response(self, model_response: ModelResponse) -> ModelResponse:
        """Convert a non-streaming ModelResponse into streaming-shaped deltas."""
        if model_response.tool_calls:
            model_response.tool_calls = _dict_tool_calls_to_deltas(  # type: ignore[assignment]
                list(model_response.tool_calls)
            )
        return model_response

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        if not self.force_non_streaming:
            # Standard streaming path (shared mixin implementation).
            yield from super().invoke_stream(*args, **kwargs)
            return
        parser = self._new_parser()
        self._log_request_snapshot(args, kwargs)
        model_response = self.invoke(*args, **kwargs)
        yield from parser.feed(self._normalize_nonstream_response(model_response))
        yield from parser.flush()
        parser.log_stream_summary()

    async def ainvoke_stream(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        if not self.force_non_streaming:
            # Standard streaming path (shared mixin implementation).
            async for out in super().ainvoke_stream(*args, **kwargs):
                yield out
            return
        parser = self._new_parser()
        self._log_request_snapshot(args, kwargs)
        model_response = await self.ainvoke(*args, **kwargs)
        for out in parser.feed(self._normalize_nonstream_response(model_response)):
            yield out
        for out in parser.flush():
            yield out
        parser.log_stream_summary()


@dataclass
class RecoveringToolCallOpenAI(_RecoveringToolCallMixin, OpenAIChat):
    """OpenAI-compatible model with streaming tool-call recovery.

    Used for the ``openai`` provider — notably Azure AI Foundry /
    OpenAI-compatible endpoints serving DeepSeek models, which can leak the
    model's native DSML tool-call markup inside ``content`` when their
    server-side streaming parser fails to convert it to native ``tool_calls``.

    Always streaming: the recovery parser runs incrementally over the
    streamed deltas (there is intentionally no non-streaming fallback flag).

    Parameters
    ----------
    tool_call_dialect:
        Name of the text dialect to detect (``"dsml"`` by default; ``None``
        or ``"none"`` disables text parsing while keeping native
        ``tool_calls`` passthrough untouched).
    """

    name: str = "RecoveringToolCallOpenAI"
    provider: str = "OpenAI"

    tool_call_dialect: Optional[str] = "dsml"

    def _request_dump_dir(self) -> Optional[str]:
        try:
            from src.shared.config.settings import get_settings

            return _resolve_request_dump_dir(get_settings().openai_debug_request_dump_path)
        except Exception:  # noqa: BLE001 - keep request path tolerant
            return None
