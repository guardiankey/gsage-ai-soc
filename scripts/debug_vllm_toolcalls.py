"""Debug harness to bisect why Qwen3 on vLLM refuses to emit tool calls.

Reproduces the exact (or shrunk) request the backend sends to vLLM and prints
verbose diagnostics for every chunk: deltas, ``finish_reason``, token usage,
recovered tool calls, raw chunk dumps.  Use it to answer three questions:

1. Does the model emit native ``tool_calls`` when forced (``tool_choice``)?
2. Does it emit them when the prompt is small?  When tools are few?
3. Does it emit them when ``enable_thinking`` is toggled?

Usage
-----
    cd /path/to/gsage-ai-soc
    source .venv/bin/activate
    python limbo/debug_vllm_toolcalls.py                       # default: full
    python limbo/debug_vllm_toolcalls.py --scenario small-prompt
    python limbo/debug_vllm_toolcalls.py --scenario all          # run all

Scenarios are designed so you can attribute the failure to one of:

    * our parser side   (we already bypass it with ``--no-parser``)
    * vLLM tool-call parser (toggle via ``--tool-call-parser`` is server-side
      only — we exercise both branches by inspecting raw deltas)
    * the model itself  (size of system prompt / number of tools)

Environment
-----------
Reads ``VLLM_BASE_URL``, ``VLLM_API_KEY``, ``VLLM_MAKER_MODEL`` (or pass
explicit ``--base-url`` / ``--model``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI

# Locate the project root so we can pull in the production system prompt
# verbatim — otherwise we would be bisecting against a guess.  We try several
# candidates so the script works whether it is run from the repo (limbo/…)
# or copied into a container (e.g. /tmp/debug_vllm_toolcalls.py with the
# code mounted under /app).
_CANDIDATE_ROOTS = [
    Path(__file__).resolve().parent.parent,  # repo: limbo/.. = repo root
    Path("/app"),                            # backend_api container layout
    Path.cwd(),                              # whatever the user is in
]
for _root in _CANDIDATE_ROOTS:
    if (_root / "src" / "backend_api").is_dir() and str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
        break

# Embedded fallback used when the project source is not importable (e.g. the
# script was copied into a container that doesn't ship the agent_factory
# module).  Kept as a verbatim short copy of the production block we are
# trying to reproduce so the bisection still works in isolation.
_EMBEDDED_DEFAULT_SYSTEM_PROMPT = """\
You are gSage AI, a cybersecurity analyst assistant.

Tool discovery & execution:
- Two execution surfaces exist, and they are NOT interchangeable:
  * CORE tools — listed in your function/tool list. Call them directly by
    their own name (e.g. ``dns_lookup(...)``).
  * DISCOVERABLE tools — every other tool. They are NOT exposed as callable
    functions. The ONLY way to run them is through the proxies
    ``run_discovered_tool`` or ``run_approved_tool``, passing the tool name
    as the ``tool_name`` argument.
- Workflow for any non-core capability:
  1. ``search_tools`` to find the tool and fetch its schema.
  2. Build ``params`` strictly from that schema.
  3. Invoke via ``run_discovered_tool`` or ``run_approved_tool``.
- MANDATORY: before saying "I can't" to any action, you MUST call
  ``search_tools`` — a matching tool may exist.

HARD RULE — discovered tools are NEVER callable by their own name. Calling
such a tool by its bare name will fail with "tool not found".

APPROVAL GATE: tools with ``requires_approval=true`` or that ask for
``_approval_summary`` MUST go through ``run_approved_tool`` with
``_approval_summary`` populated in the user's language.
"""

try:
    from src.backend_api.app.services.agent_factory import (  # noqa: E402
        _DEFAULT_SYSTEM_PROMPT,
    )
    _PROMPT_SOURCE = "imported from src.backend_api.app.services.agent_factory"
except ModuleNotFoundError:
    _DEFAULT_SYSTEM_PROMPT = _EMBEDDED_DEFAULT_SYSTEM_PROMPT
    _PROMPT_SOURCE = (
        "EMBEDDED FALLBACK (project src/ not importable — "
        "results from this script will not match production exactly)"
    )


# ---------------------------------------------------------------------------
# Tool catalog — names come straight from the production log we are debugging.
# Schemas are intentionally minimal but valid so the model treats them as real.
# ---------------------------------------------------------------------------

PROXY_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_tools",
            "description": (
                "Search the discoverable tool catalog by free-text query and "
                "return matching tool names with their JSON schemas."
            ),
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Free-text search."},
                    "limit": {"type": "integer", "default": 10},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_discovered_tool",
            "description": (
                "Execute a non-sensitive tool discovered via search_tools. "
                "Only for tools with requires_approval=false."
            ),
            "parameters": {
                "type": "object",
                "required": ["tool_name", "params"],
                "properties": {
                    "tool_name": {"type": "string"},
                    "params": {"type": "object"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_approved_tool",
            "description": (
                "Execute an approval-gated tool. params MUST include _approval_summary."
            ),
            "parameters": {
                "type": "object",
                "required": ["tool_name", "params"],
                "properties": {
                    "tool_name": {"type": "string"},
                    "params": {
                        "type": "object",
                        "required": ["_approval_summary"],
                        "properties": {
                            "_approval_summary": {"type": "string", "minLength": 1},
                        },
                    },
                },
                "additionalProperties": False,
            },
        },
    },
]

# The 15 extra "core" tools advertised in the production log.  Schemas are
# placeholders — only names/shape matter for the bisection.
EXTRA_CORE_TOOL_NAMES = [
    "add_to_knowledge_base",
    "change_file_scope",
    "delete_from_knowledge_base",
    "dns_lookup",
    "domain_security_audit",
    "eml_analyzer",
    "generate_document",
    "knowledge_base",
    "list_recent_artifacts",
    "mermaid_reference",
    "mermaid_validate",
    "nmap_scan",
    "read_file",
    "search_knowledge_base",
    "threat_intel_lookup",
]


def _placeholder_tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} (placeholder schema for bisection harness).",
            "parameters": {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "additionalProperties": True,
            },
        },
    }


EXTRA_CORE_TOOLS = [_placeholder_tool(n) for n in EXTRA_CORE_TOOL_NAMES]
FULL_TOOLS = PROXY_TOOLS + EXTRA_CORE_TOOLS  # 18 tools, matches production log


# ---------------------------------------------------------------------------
# Context blocks reproduced from the real conversation
# ---------------------------------------------------------------------------

DEPARTMENT_BLOCK = (
    "[DEPARTMENT_CONTEXT]\n"
    "The user's active department is: Default (ID: a083c881-c1dc-418e-a4a5-f8022a067298)\n"
    "All department-scoped operations (datastores, files, tool configs) must "
    "use this department context automatically. Do NOT ask the user to define a "
    "department.\n"
    "[/DEPARTMENT_CONTEXT]"
)

# Short variant of the system prompt — keeps only the tool-discovery rules.
SHORT_SYSTEM_PROMPT = """\
You are gSage AI. Use the proxy tools to execute discoverable tools:

- `search_tools(query)` to discover a tool and fetch its schema.
- `run_discovered_tool(tool_name, params)` for non-sensitive tools.
- `run_approved_tool(tool_name, params)` for tools that require approval
  (params MUST include `_approval_summary`).

When the user asks to "list tools", call `search_tools` with a broad query.
"""


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def build_messages(system_prompt: str, history: bool = True) -> list[dict]:
    """Reproduce the conversation shape from the production log."""
    msgs: list[dict] = [{"role": "system", "content": system_prompt}]
    if history:
        msgs.extend(
            [
                {"role": "user", "content": "Olá"},
                {"role": "assistant", "content": "Olá! Como posso ajudar você hoje?"},
                {"role": "user", "content": "liste tools"},
                {
                    "role": "assistant",
                    "content": "Vou listar as ferramentas disponíveis para você.",
                },
                {"role": "user", "content": "liste tools novamente"},
                {
                    "role": "assistant",
                    "content": "Vou buscar as ferramentas disponíveis no sistema.",
                },
            ]
        )
    msgs.append(
        {
            "role": "user",
            "content": f"{DEPARTMENT_BLOCK}\n\n---\nliste tools novamente",
        }
    )
    return msgs


SCENARIOS: Dict[str, Dict[str, Any]] = {
    "production": {
        "desc": "Reproduces production: full prompt + 18 tools + 4 prior turns.",
        "system_prompt": _DEFAULT_SYSTEM_PROMPT,
        "tools": FULL_TOOLS,
        "history": True,
        "tool_choice": "auto",
    },
    "small-prompt": {
        "desc": "Short prompt, same 18 tools.  Isolates prompt-size effect.",
        "system_prompt": SHORT_SYSTEM_PROMPT,
        "tools": FULL_TOOLS,
        "history": True,
        "tool_choice": "auto",
    },
    "few-tools": {
        "desc": "Full prompt, only 3 proxy tools.  Isolates tool-count effect.",
        "system_prompt": _DEFAULT_SYSTEM_PROMPT,
        "tools": PROXY_TOOLS,
        "history": True,
        "tool_choice": "auto",
    },
    "no-history": {
        "desc": "Full prompt + 18 tools but NO prior assistant turns. "
                "Isolates auto-prime from past narrations.",
        "system_prompt": _DEFAULT_SYSTEM_PROMPT,
        "tools": FULL_TOOLS,
        "history": False,
        "tool_choice": "auto",
    },
    "force-required": {
        "desc": "Full prompt + 18 tools but tool_choice='required'. "
                "Proves whether the model *can* call tools at all.",
        "system_prompt": _DEFAULT_SYSTEM_PROMPT,
        "tools": FULL_TOOLS,
        "history": True,
        "tool_choice": "required",
    },
    "minimal": {
        "desc": "Short prompt + 3 tools + no history.  Easiest path to a call.",
        "system_prompt": SHORT_SYSTEM_PROMPT,
        "tools": PROXY_TOOLS,
        "history": False,
        "tool_choice": "auto",
    },
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _summarize_messages(messages: list[dict]) -> dict:
    roles: Counter[str] = Counter(m["role"] for m in messages)
    total_chars = sum(len(m.get("content", "")) for m in messages if isinstance(m.get("content"), str))
    return {"count": len(messages), "roles": dict(roles), "content_chars": total_chars}


# Fields accepted by the vLLM OpenAI-compatible endpoint per role. Anything
# else (Agno-specific fields like ``metrics``, ``references``, ``created_at``,
# ``temporary``, plus ``name=None`` placeholders) is stripped before replay.
_OPENAI_MSG_FIELDS_BY_ROLE = {
    "system":    {"role", "content", "name"},
    "developer": {"role", "content", "name"},
    "user":      {"role", "content", "name"},
    "assistant": {"role", "content", "name", "tool_calls", "refusal", "audio"},
    "tool":      {"role", "content", "tool_call_id"},
    "function":  {"role", "content", "name"},
}
_OPENAI_TOOL_CALL_FIELDS = {"id", "type", "function", "index"}


def _sanitize_tool_call(tc: Any) -> Optional[dict]:
    if not isinstance(tc, dict):
        return None
    out: Dict[str, Any] = {k: v for k, v in tc.items() if k in _OPENAI_TOOL_CALL_FIELDS and v is not None}
    out.setdefault("type", "function")
    fn = out.get("function")
    if isinstance(fn, dict):
        out["function"] = {k: v for k, v in fn.items() if k in {"name", "arguments"} and v is not None}
        out["function"].setdefault("arguments", "")
    return out or None


def _sanitize_messages_for_openai(messages: list[dict]) -> list[dict]:
    """Strip Agno-only fields and ``None`` placeholders so vLLM accepts the dump.

    The backend dumps each message via ``model_dump`` on Agno's ``Message``,
    which carries extra fields (``metrics``, ``references``, ``created_at``,
    ``temporary``, ``name=None``...). vLLM validates strictly against the
    OpenAI ``ChatCompletion*MessageParam`` schemas and rejects them.
    """
    cleaned: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        allowed = _OPENAI_MSG_FIELDS_BY_ROLE.get(role or "", {"role", "content", "name"})
        out: Dict[str, Any] = {}
        for k in allowed:
            if k not in m:
                continue
            v = m[k]
            if v is None:
                continue
            if k == "tool_calls" and isinstance(v, list):
                tcs = [tc for tc in (_sanitize_tool_call(t) for t in v) if tc]
                if tcs:
                    out[k] = tcs
                continue
            out[k] = v
        # Assistant messages must have at least content or tool_calls.
        if role == "assistant" and "content" not in out and "tool_calls" not in out:
            out["content"] = ""
        # Tool messages require a string tool_call_id.
        if role == "tool" and not out.get("tool_call_id"):
            continue
        out["role"] = role
        cleaned.append(out)
    return cleaned


def _print_kv(label: str, value: Any) -> None:
    print(f"  {label:<22} {value}")


def run_scenario(
    *,
    name: str,
    scenario: Dict[str, Any],
    base_url: str,
    api_key: str,
    model_id: str,
    enable_thinking: Optional[bool],
    streaming: bool,
    dump_raw: bool,
) -> Dict[str, Any]:
    print()
    print("=" * 78)
    print(f"SCENARIO: {name}")
    print(f"  {scenario['desc']}")
    print("=" * 78)

    messages = build_messages(scenario["system_prompt"], history=scenario["history"])
    tools = scenario["tools"]
    tool_choice = scenario["tool_choice"]

    msg_summary = _summarize_messages(messages)
    print("REQUEST")
    _print_kv("model", model_id)
    _print_kv("base_url", base_url)
    _print_kv("streaming", streaming)
    _print_kv("enable_thinking", enable_thinking)
    _print_kv("messages", msg_summary)
    _print_kv("tools.count", len(tools))
    _print_kv("tools.names", [t["function"]["name"] for t in tools])
    _print_kv("tool_choice", tool_choice)

    client = OpenAI(base_url=base_url, api_key=api_key)

    extra_body: Dict[str, Any] = {}
    if enable_thinking is not None:
        extra_body["chat_template_kwargs"] = {"enable_thinking": enable_thinking}

    start = time.perf_counter()
    if streaming:
        result = _run_streaming(
            client, model_id, messages, tools, tool_choice, extra_body, dump_raw
        )
    else:
        result = _run_nonstreaming(
            client, model_id, messages, tools, tool_choice, extra_body
        )
    elapsed = time.perf_counter() - start

    print()
    print("RESPONSE")
    _print_kv("elapsed_s", f"{elapsed:.2f}")
    for k, v in result.items():
        _print_kv(k, v)
    return result


def _run_streaming(
    client: OpenAI,
    model_id: str,
    messages: list[dict],
    tools: list[dict],
    tool_choice: Any,
    extra_body: Dict[str, Any],
    dump_raw: bool,
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    create_kwargs: Dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "stream": True,
        "stream_options": {"include_usage": True},
        "extra_body": extra_body or None,
    }
    # Merge captured extras (temperature, top_p, max_tokens, seed,
    # response_format, …) without overriding the core fields above.
    for k, v in (extras or {}).items():
        if k in create_kwargs or v is None:
            continue
        create_kwargs[k] = v
    stream = client.chat.completions.create(**create_kwargs)  # type: ignore[arg-type]

    text_chunks: List[str] = []
    reasoning_chunks: List[str] = []
    tool_calls_acc: Dict[int, Dict[str, Any]] = {}
    finish_reason: Optional[str] = None
    usage = None
    n_chunks = 0

    for chunk in stream:
        n_chunks += 1
        if dump_raw:
            print(f"  RAW#{n_chunks}: {chunk.model_dump_json(exclude_none=True)}")
        if chunk.choices:
            ch = chunk.choices[0]
            d = ch.delta
            if getattr(d, "content", None):
                text_chunks.append(d.content)
            if getattr(d, "reasoning_content", None):
                reasoning_chunks.append(d.reasoning_content)
            elif getattr(d, "reasoning", None):
                reasoning_chunks.append(d.reasoning)
            if getattr(d, "tool_calls", None):
                for tc in d.tool_calls:
                    slot = tool_calls_acc.setdefault(
                        tc.index, {"id": None, "name": "", "arguments": ""}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
            if ch.finish_reason:
                finish_reason = ch.finish_reason
        if chunk.usage:
            usage = chunk.usage

    text = "".join(text_chunks)
    reasoning = "".join(reasoning_chunks)
    tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]

    return {
        "n_chunks": n_chunks,
        "finish_reason": finish_reason,
        "input_tokens": usage.prompt_tokens if usage else None,
        "output_tokens": usage.completion_tokens if usage else None,
        "text_chars": len(text),
        "text_head": text[:240],
        "reasoning_chars": len(reasoning),
        "reasoning_head": reasoning[:240],
        "tool_calls_count": len(tool_calls),
        "tool_calls": [
            {"name": tc["name"], "arguments": tc["arguments"][:240]} for tc in tool_calls
        ],
    }


def _run_nonstreaming(
    client: OpenAI,
    model_id: str,
    messages: list[dict],
    tools: list[dict],
    tool_choice: Any,
    extra_body: Dict[str, Any],
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    create_kwargs: Dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "stream": False,
        "extra_body": extra_body or None,
    }
    for k, v in (extras or {}).items():
        if k in create_kwargs or v is None:
            continue
        create_kwargs[k] = v
    rsp = client.chat.completions.create(**create_kwargs)  # type: ignore[arg-type]
    choice = rsp.choices[0]
    msg = choice.message
    tcs = msg.tool_calls or []
    return {
        "finish_reason": choice.finish_reason,
        "input_tokens": rsp.usage.prompt_tokens if rsp.usage else None,
        "output_tokens": rsp.usage.completion_tokens if rsp.usage else None,
        "text_chars": len(msg.content or ""),
        "text_head": (msg.content or "")[:240],
        "reasoning_chars": len(getattr(msg, "reasoning_content", "") or ""),
        "reasoning_head": (getattr(msg, "reasoning_content", "") or "")[:240],
        "tool_calls_count": len(tcs),
        "tool_calls": [
            {"name": tc.function.name, "arguments": (tc.function.arguments or "")[:240]}
            for tc in tcs
        ],
    }


def run_replay(
    *,
    replay_path: str,
    base_url: str,
    api_key: str,
    model_id: str,
    enable_thinking_override: Optional[bool],
    streaming: bool,
    dump_raw: bool,
    no_extra_body: bool = False,
    no_tool_choice: bool = False,
    strip_extras: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Replay a request payload dumped by the backend (Phase 1 diagnostic).

    The dump file is produced by ``RecoveringToolCallVLLM`` when
    ``VLLM_DEBUG_REQUEST_DUMP_PATH`` is set.  It contains the exact
    messages/tools/tool_choice/extra_body sent to vLLM, so replaying it from
    this script proves whether the failure reproduces *outside* the Agno
    pipeline (\u2192 it's the request shape) or only inside it (\u2192 it's an
    adapter bug).
    """
    print()
    print("=" * 78)
    print(f"REPLAY: {replay_path}")
    print("=" * 78)

    with open(replay_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    raw_messages = payload.get("messages") or []
    messages = _sanitize_messages_for_openai(raw_messages)
    tools = payload.get("tools") or []
    tool_choice = payload.get("tool_choice")
    extra_body = payload.get("extra_body") or {}
    # Extras captured from Agno's get_request_params() (temperature, top_p,
    # max_tokens, seed, response_format, service_tier, …).  These are the
    # parameters most likely to flip the model into a different output mode.
    final_extras = payload.get("final_request_extras") or {}
    if strip_extras:
        final_extras = {k: v for k, v in final_extras.items() if k not in strip_extras}
    # tool_choice defaults to "auto" if the captured value is None and tools exist.
    if tool_choice is None and tools and not no_tool_choice:
        tool_choice = "auto"
    if no_tool_choice:
        tool_choice = None  # mirror prod: omit tool_choice from the request

    if enable_thinking_override is not None:
        extra_body = dict(extra_body)
        extra_body["chat_template_kwargs"] = {"enable_thinking": enable_thinking_override}

    # Simulate production (Agno's VLLM with enable_thinking=None drops
    # chat_template_kwargs entirely).  Useful to prove that the missing
    # extra_body is what breaks native tool_calls.
    if no_extra_body:
        extra_body = {}

    dumped_model = payload.get("model_id")
    effective_model = dumped_model or model_id

    print("REQUEST (from dump)")
    _print_kv("dump_captured_at", payload.get("captured_at"))
    _print_kv("dump_model_id", dumped_model)
    _print_kv("effective_model", effective_model)
    _print_kv("base_url", base_url)
    _print_kv("streaming", streaming)
    _print_kv("enable_thinking_in_dump", payload.get("enable_thinking"))
    _print_kv("messages", _summarize_messages(messages))
    _print_kv(
        "messages.sanitized",
        f"{len(raw_messages)} dumped -> {len(messages)} sent to vLLM",
    )
    _print_kv("tools.count", len(tools))
    _print_kv(
        "tools.names",
        [t.get("function", {}).get("name") or t.get("name") for t in tools],
    )
    _print_kv("tool_choice", tool_choice)
    _print_kv("extra_body_keys", sorted(extra_body.keys()) if isinstance(extra_body, dict) else None)
    _print_kv("extra_body", json.dumps(extra_body, ensure_ascii=False) if extra_body else None)
    _print_kv(
        "final_extras",
        json.dumps(final_extras, ensure_ascii=False) if final_extras else None,
    )
    # System prompt head: helps spot template/wrapper differences vs. prod.
    sys_msg = next((m for m in messages if m.get("role") in {"system", "developer"}), None)
    sys_content = (sys_msg or {}).get("content") if isinstance(sys_msg, dict) else None
    if isinstance(sys_content, str):
        _print_kv("system_head", sys_content[:200].replace("\n", " \u23ce "))

    client = OpenAI(base_url=base_url, api_key=api_key)
    start = time.perf_counter()
    if streaming:
        result = _run_streaming(
            client, effective_model, messages, tools, tool_choice, extra_body, dump_raw,
            extras=final_extras,
        )
    else:
        result = _run_nonstreaming(
            client, effective_model, messages, tools, tool_choice, extra_body,
            extras=final_extras,
        )
    elapsed = time.perf_counter() - start

    print()
    print("RESPONSE")
    _print_kv("elapsed_s", f"{elapsed:.2f}")
    for k, v in result.items():
        _print_kv(k, v)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()) + ["all"],
        default="production",
        help="Which scenario to run (default: production).",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("VLLM_BASE_URL", "http://10.61.1.60:8000/v1"),
    )
    parser.add_argument("--api-key", default=os.environ.get("VLLM_API_KEY", "EMPTY"))
    parser.add_argument(
        "--model",
        default=os.environ.get("VLLM_MAKER_MODEL", "nvidia/Qwen3.6-35B-A3B-NVFP4"),
    )
    parser.add_argument(
        "--enable-thinking",
        choices=["true", "false", "unset"],
        default="false",
        help="chat_template_kwargs.enable_thinking; 'unset' omits the field.",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Run non-streaming (closer to a normal openai.complete call).",
    )
    parser.add_argument(
        "--dump-raw",
        action="store_true",
        help="Print every raw streaming chunk as JSON (verbose).",
    )
    parser.add_argument(
        "--replay",
        metavar="FILE",
        default=None,
        help=(
            "Replay a previously dumped request JSON (produced by the "
            "backend with VLLM_DEBUG_REQUEST_DUMP_PATH set).  When this is "
            "used, --scenario is ignored."
        ),
    )
    parser.add_argument(
        "--no-extra-body",
        action="store_true",
        help=(
            "Strip extra_body entirely from the replayed request. "
            "Reproduces Agno's VLLM behavior when enable_thinking is None "
            "(no chat_template_kwargs sent)."
        ),
    )
    parser.add_argument(
        "--no-tool-choice",
        action="store_true",
        help=(
            "Omit tool_choice from the request entirely. Reproduces what "
            "Agno sends when no explicit tool_choice was configured. Useful "
            "to test whether vLLM only engages its tool parser when "
            "tool_choice is present."
        ),
    )
    parser.add_argument(
        "--strip-extra",
        action="append",
        default=[],
        metavar="KEY",
        help=(
            "Drop a key from final_request_extras before replay. Repeat to "
            "strip multiple. Use to bisect sampling params (e.g. "
            "--strip-extra presence_penalty)."
        ),
    )
    args = parser.parse_args()

    et: Optional[bool]
    if args.enable_thinking == "true":
        et = True
    elif args.enable_thinking == "false":
        et = False
    else:
        et = None

    if args.replay:
        try:
            res = run_replay(
                replay_path=args.replay,
                base_url=args.base_url,
                api_key=args.api_key,
                model_id=args.model,
                enable_thinking_override=et if args.enable_thinking != "unset" else None,
                streaming=not args.no_stream,
                dump_raw=args.dump_raw,
                no_extra_body=args.no_extra_body,
                no_tool_choice=args.no_tool_choice,
                strip_extras=args.strip_extra or None,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  !! replay raised: {exc!r}")
            return 1
        return 0 if res else 1

    scenarios = list(SCENARIOS.keys()) if args.scenario == "all" else [args.scenario]
    print(f"System prompt source: {_PROMPT_SOURCE}")
    print(f"  prompt size: {len(_DEFAULT_SYSTEM_PROMPT)} chars")
    all_results: list[tuple[str, dict]] = []
    for name in scenarios:
        try:
            res = run_scenario(
                name=name,
                scenario=SCENARIOS[name],
                base_url=args.base_url,
                api_key=args.api_key,
                model_id=args.model,
                enable_thinking=et,
                streaming=not args.no_stream,
                dump_raw=args.dump_raw,
            )
            all_results.append((name, res))
        except Exception as exc:  # noqa: BLE001 - bench script, surface errors
            print(f"  !! scenario {name} raised: {exc!r}")
            all_results.append((name, {"error": repr(exc)}))

    if len(all_results) > 1:
        print()
        print("=" * 78)
        print("BISECTION SUMMARY (smaller = easier path to a tool call)")
        print("=" * 78)
        print(
            f"  {'scenario':<18} {'finish':<10} {'in_tok':>7} {'out_tok':>8} "
            f"{'text':>6} {'calls':>5}  first_call"
        )
        for name, r in all_results:
            if "error" in r:
                print(f"  {name:<18} ERROR: {r['error']}")
                continue
            first = (r.get("tool_calls") or [{}])[0].get("name", "—")
            print(
                f"  {name:<18} {str(r.get('finish_reason')):<10} "
                f"{str(r.get('input_tokens') or '—'):>7} "
                f"{str(r.get('output_tokens') or '—'):>8} "
                f"{r.get('text_chars', 0):>6} "
                f"{r.get('tool_calls_count', 0):>5}  {first}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
