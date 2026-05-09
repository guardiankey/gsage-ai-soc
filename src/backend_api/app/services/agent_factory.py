"""gSage AI — Agent factory with tenant context injection.

Usage::

    from src.backend_api.app.services.agent_factory import build_agent, AGENT_REGISTRY

    agent = build_agent(
        ctx=tenant_context,
        agent_id="cybersecurity",
        session_id=session_id,   # from ctx.build_session_id(...)
        org=org,                 # optional GSageOrganization from DB
    )
    run_output = await agent.arun(user_message)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any, Optional
from uuid import uuid4

from agno.agent import Agent
from agno.db.postgres.async_postgres import AsyncPostgresDb
from agno.media import Image
from agno.tools.function import Function, ToolResult
from agno.tools.mcp import MCPTools
from agno.tools.mcp.params import StreamableHTTPClientParams
from mcp.types import EmbeddedResource, ImageContent, TextContent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config.settings import get_settings
from src.backend_api.app.services.knowledge import build_knowledge
from src.backend_api.app.services.knowledge_tools import KnowledgeToolkit
from src.shared.models.interface_profile import GSageInterfaceProfile
import logging

if TYPE_CHECKING:
    from src.backend_api.app.core.tenant import TenantContext
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.user import GSageUser

log = logging.getLogger(__name__)


def _patch_agno_unknown_tool_message() -> None:
    """Replace Agno's generic "tool does not exist" error with a hint that
    routes the LLM to the proxy executors.

    Agno's stock message ("Error: The requested tool does not exist or is
    not available.") gives the model no clue that discoverable tools are
    only callable through ``run_discovered_tool`` / ``run_approved_tool``.
    When the model hallucinates a bare-name call (e.g. ``send_email_direct``
    instead of ``run_discovered_tool(tool_name="send_email_direct", ...)``),
    the message we return here lets it self-correct on the next turn.
    """
    from agno.models import base as _agno_base
    from agno.models.message import Message as _AgnoMessage
    from agno.tools.function import FunctionCall as _FunctionCall
    from agno.utils.tools import get_function_call_for_tool_call

    if getattr(_agno_base.Model.get_function_calls_to_run, "_gsage_patched", False):
        return

    _orig = _agno_base.Model.get_function_calls_to_run

    def _patched(self, assistant_message, messages, functions=None):  # type: ignore[no-untyped-def]
        function_calls_to_run: list[_FunctionCall] = []
        if assistant_message.tool_calls is not None:
            for tool_call in assistant_message.tool_calls:
                _tool_call_id = tool_call.get("id")
                _fn = tool_call.get("function", {}) or {}
                _tool_call_name = _fn.get("name") or ""
                _function_call = get_function_call_for_tool_call(tool_call, functions)
                if _function_call is None:
                    is_proxy = _tool_call_name in (
                        "run_discovered_tool",
                        "run_approved_tool",
                        "search_tools",
                    )
                    if is_proxy:
                        hint = (
                            f"Error: tool '{_tool_call_name}' is not registered for this agent."
                        )
                    else:
                        hint = (
                            f"Error: '{_tool_call_name}' is not a directly callable function. "
                            "Discoverable tools (anything found via `search_tools`) MUST be "
                            "invoked through the proxy executors. Retry like this:\n"
                            "  • If requires_approval=false → "
                            f"run_discovered_tool(tool_name=\"{_tool_call_name}\", params={{...}})\n"
                            "  • If requires_approval=true  → "
                            f"run_approved_tool(tool_name=\"{_tool_call_name}\", "
                            "params={..., \"_approval_summary\": \"...\"})\n"
                            f"If you have not yet fetched the schema, call "
                            f"search_tools(query=\"{_tool_call_name}\") first."
                        )
                    messages.append(
                        _AgnoMessage(
                            role=self.tool_message_role,
                            tool_call_id=_tool_call_id,
                            tool_name=_tool_call_name,
                            content=hint,
                        )
                    )
                    continue
                if _function_call.error is not None:
                    messages.append(
                        _AgnoMessage(
                            role=self.tool_message_role,
                            tool_call_id=_tool_call_id,
                            tool_name=_tool_call_name,
                            content=_function_call.error,
                        )
                    )
                    continue
                function_calls_to_run.append(_function_call)
        return function_calls_to_run

    _patched._gsage_patched = True  # type: ignore[attr-defined]
    _agno_base.Model.get_function_calls_to_run = _patched  # type: ignore[assignment]
    log.info("agno patch installed: helpful unknown-tool message")


def _patch_agno_session_history_includes_paused() -> None:
    """Include ``paused`` runs in the conversation history fed to the LLM.

    Agno's ``AgentSession.get_messages`` defaults ``skip_statuses`` to
    ``[paused, cancelled, error]``, so any run that paused waiting for
    approval (HITL) or for a background task is silently dropped from the
    history on the *next* user turn.  In our HITL flow that turn never
    runs ``acontinue_run`` synchronously — the worker resumes the paused
    run later — so the user can keep chatting in the meantime; without
    this patch the LLM loses every message exchanged before the pause and
    behaves as if the conversation just started.

    We narrow the default to ``[cancelled, error]`` only.  ``paused`` runs
    carry valid user/assistant/tool messages that the LLM SHOULD see.
    """
    from agno.models.message import Message
    from agno.run.base import RunStatus
    from agno.session.agent import AgentSession

    if getattr(AgentSession.get_messages, "_gsage_patched", False):
        return

    _orig_get_messages = AgentSession.get_messages

    def _stub_orphan_tool_calls(messages):  # type: ignore[no-untyped-def]
        """Inject synthetic ``tool`` messages for any ``tool_call`` that has
        no matching response further down the list.

        Without this, when a paused (HITL) run enters the history its
        assistant message carrying ``tool_calls`` reaches the LLM with no
        corresponding ``tool`` reply, and OpenAI rejects the request with
        ``invalid_request_error: insufficient tool messages following
        tool_calls message``.

        We collect every ``tool_call_id`` that already has a matching
        ``tool`` message in the list (regardless of position — agno keeps
        them in order) and, for every assistant message with unanswered
        tool_calls, append synthetic stubs immediately after it.
        """
        if not messages:
            return messages
        answered_ids = {
            m.tool_call_id
            for m in messages
            if m.role == "tool" and m.tool_call_id
        }
        result = []
        for msg in messages:
            result.append(msg)
            if msg.role != "assistant" or not msg.tool_calls:
                continue
            for tc in msg.tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if not tc_id or tc_id in answered_ids:
                    continue
                fn = (
                    tc.get("function", {}).get("name")
                    if isinstance(tc, dict)
                    else None
                ) or "unknown_tool"
                stub = Message(
                    role="tool",
                    tool_call_id=tc_id,
                    tool_name=fn,
                    content=(
                        "[pending] This tool call has not been executed yet "
                        "(awaiting human approval or background task "
                        "completion). Do not retry it; the system will "
                        "report the result automatically once it is "
                        "resolved."
                    ),
                    from_history=getattr(msg, "from_history", False),
                )
                answered_ids.add(tc_id)
                result.append(stub)
        return result

    def _patched_get_messages(  # type: ignore[no-untyped-def]
        self,
        agent_id=None,
        team_id=None,
        last_n_runs=None,
        limit=None,
        skip_roles=None,
        skip_statuses=None,
        skip_history_messages=True,
    ):
        # Only override the default; explicit caller intent wins.
        override_default = skip_statuses is None
        if override_default:
            skip_statuses = [RunStatus.cancelled, RunStatus.error]
        messages = _orig_get_messages(
            self,
            agent_id=agent_id,
            team_id=team_id,
            last_n_runs=last_n_runs,
            limit=limit,
            skip_roles=skip_roles,
            skip_statuses=skip_statuses,
            skip_history_messages=skip_history_messages,
        )
        # Stub orphan tool_calls only when paused runs may be present.
        if override_default:
            messages = _stub_orphan_tool_calls(messages)
        return messages

    _patched_get_messages._gsage_patched = True  # type: ignore[attr-defined]
    AgentSession.get_messages = _patched_get_messages  # type: ignore[assignment]
    log.info("agno patch installed: paused runs included in chat history")


def _patch_agno_continue_run_messages_dedup() -> None:
    """Avoid duplicating the paused run's messages on ``acontinue_run``.

    ``get_continue_run_messages`` re-appends the saved messages of the
    paused run via ``input`` AFTER calling ``session.get_messages`` to
    fetch history.  Our previous patch made ``get_messages`` include
    paused runs by default, which is the right behaviour for fresh
    ``arun`` calls but causes the run-being-continued to appear twice
    here.

    We patch ``get_continue_run_messages`` to call ``get_messages`` with
    the *original* default ``skip_statuses`` (excluding paused), so the
    paused run only enters the message list through ``input``.
    """
    from agno.agent import _messages as _agno_messages
    from agno.run.base import RunStatus

    if getattr(_agno_messages.get_continue_run_messages, "_gsage_patched", False):
        return

    _OriginalRunStatus = RunStatus  # capture for closure

    def _patched(  # type: ignore[no-untyped-def]
        agent,
        input,
        session=None,
        add_history_to_context=None,
        run_context=None,
    ):
        from copy import deepcopy

        from agno.run.messages import RunMessages
        from agno.utils.log import log_debug

        run_messages = RunMessages()

        if add_history_to_context is None:
            add_history_to_context = agent.add_history_to_context

        user_message = None
        for msg in reversed(input):
            if msg.role == agent.user_message_role:
                user_message = msg
                break
        system_message = None
        for msg in input:
            if msg.role == agent.system_message_role:
                system_message = msg
                break
        run_messages.system_message = system_message
        run_messages.user_message = user_message

        input_has_history = any(getattr(msg, "from_history", False) for msg in input)

        if system_message is not None:
            run_messages.messages.append(system_message)

        if add_history_to_context and session is not None and not input_has_history:
            skip_role = (
                agent.system_message_role
                if agent.system_message_role not in ["user", "assistant", "tool"]
                else None
            )
            history = session.get_messages(
                last_n_runs=agent.num_history_runs,
                limit=agent.num_history_messages,
                skip_roles=[skip_role] if skip_role else None,
                # Exclude the paused run we are about to re-inject via ``input``
                # to prevent duplication.  Cancelled/error runs stay excluded
                # for the same reason as upstream (no useful signal).
                skip_statuses=[
                    _OriginalRunStatus.paused,
                    _OriginalRunStatus.cancelled,
                    _OriginalRunStatus.error,
                ],
                agent_id=agent.id if agent.team_id is not None else None,
            )

            if len(history) > 0:
                history_copy = [deepcopy(msg) for msg in history]
                for _msg in history_copy:
                    _msg.from_history = True
                if agent.max_tool_calls_from_history is not None:
                    from agno.utils.message import filter_tool_calls

                    filter_tool_calls(history_copy, agent.max_tool_calls_from_history)
                log_debug(f"Adding {len(history_copy)} messages from history")
                run_messages.messages += history_copy

        for msg in input:
            if msg is not system_message:
                run_messages.messages.append(msg)

        if run_context is not None:
            run_context.messages = run_messages.messages

        return run_messages

    _patched._gsage_patched = True  # type: ignore[attr-defined]
    _agno_messages.get_continue_run_messages = _patched  # type: ignore[assignment]
    log.info("agno patch installed: continue_run history dedup")


_patch_agno_unknown_tool_message()
_patch_agno_session_history_includes_paused()
_patch_agno_continue_run_messages_dedup()


# ---------------------------------------------------------------------------
# Mermaid diagram instructions — shared across channels that support rendering
# ---------------------------------------------------------------------------

_MERMAID_PROMPT = (
    "## Mermaid diagrams\n"
    "Supported: flowchart, sequenceDiagram, classDiagram, stateDiagram-v2, "
    "erDiagram, journey, gantt, pie, mindmap, timeline, sankey, "
    "xychart(-beta), packet, kanban, architecture-beta, radar-beta, block. "
    "Do NOT use: barChart, packet-beta, block-beta, zenuml "
    "(zenuml is not bundled in the web renderer — use `sequenceDiagram`).\n"
    "The `-beta`/`-v2` suffix or 'in development' status does NOT disqualify "
    "a type — the list above is authoritative. Use any type on it freely; "
    "only refuse types explicitly listed as 'Do NOT use'.\n"
    "MANDATORY workflow: (1) call `mermaid_reference` for the target type; "
    "(2) draft; (3) call `mermaid_validate` with the draft (no ``` fences) "
    "and fix until `is_valid=true`; (4) only then output inside ```mermaid. "
    "Never show an unvalidated diagram.\n"
    "Use `return_image=true` ONLY when the user asks for a downloadable image "
    "or the channel can't render Mermaid (email, Telegram, CLI). Web renders "
    "natively — don't request PNGs there.\n"
    "Fragile types — read the reference's ❌ anti-examples BEFORE drafting:\n"
    "  • block-beta: no labels on composite groups; no [(DB)] shapes inside "
    "    composite groups; no \\n in labels; keep it flat when in doubt.\n"
    "  • requirementDiagram: always quote text:/type:/docRef: values; "
    "    risk/verifymethod are case-sensitive (High/Test, not high/test); "
    "    `{` on the same line as the header; no blank lines in blocks.\n"
    "Sankey is a DAG: dedupe bidirectional flows and avoid synthetic roots "
    "that reappear as destinations. When data comes from `pcap_analyzer` "
    "(overview/flows), use its `sankey_hint.mermaid` as-is — do NOT rebuild "
    "from top_conversations/tcp_conversations/udp_conversations."
)

# ---------------------------------------------------------------------------
# Default channel-specific prompt additions (applied when no profile configured)
# Order of system prompt composition:
#   base → org.system_prompt → user.ai_instructions → channel/org → channel/user
# ---------------------------------------------------------------------------

_CHANNEL_DEFAULT_PROMPTS: dict[str, str] = {
    "email": (
        "# Channel: Email\n"
        "Professional email reply. Clear paragraphs, complete sentences, "
        "minimal markdown. Numbered lists OK for step-by-step. Keep focused."
    ),
    "whatsapp": (
        "# Channel: WhatsApp\n"
        "SHORT and conversational (1–3 sentences). Plain text, no markdown "
        "headers/lists. Use simple dashes (-) if listing."
    ),
    "telegram": (
        "# Channel: Telegram\n"
        "Concise (2–5 sentences). PLAIN TEXT ONLY — no *, _, `, #, bold, "
        "italic, code blocks. Use dashes (-) for lists. No filler."
    ),
    "slack": (
        "# Channel: Slack\n"
        "Slack markdown (*bold*, _italic_, `code`, ```blocks```). Focused, "
        "sparse bullets, avoid long responses."
    ),
    "cli": (
        "# Channel: CLI\n"
        "Terse, technical. No pleasantries. Plain text; code blocks for "
        "commands or file contents."
    ),
    "api": (
        "# Channel: API\n"
        "Concise, structured, no filler. Numbered lists / clear sections, "
        "no markdown headers. Mermaid inside ```mermaid allowed — client "
        "may render.\n"
        + _MERMAID_PROMPT
    ),
    "web": (
        "# Channel: Web\n"
        "Mermaid inside ```mermaid renders as interactive SVG. Use only "
        "when a diagram adds clarity.\n"
        + _MERMAID_PROMPT
    ),
    "scheduled": "",  # Scheduled jobs — no channel override
}

# ---------------------------------------------------------------------------
# Default system prompt (cybersecurity focus)
# ---------------------------------------------------------------------------

_DEFAULT_SYSTEM_PROMPT = """\
You are gSage AI, a cybersecurity analyst assistant. Expertise: network security
(DNS, WHOIS, port scanning, IP reputation), threat intel/OSINT, vulnerability
assessment, incident response, log analysis, hardening.

Guidelines:
- Validate/sanitize inputs. Use MCP tools for domains/IPs/URLs.
- Structured output with severity ratings; tables for multiple findings.
- Cite sources (tool output, CVE IDs). Never expose credentials/tokens.
- If a request is outside the security toolset, say so.

Tool execution & HITL (human-in-the-loop):
- ALWAYS call tools directly — do NOT ask for manual approval in chat.
- The system intercepts sensitive calls and creates an approval record for
  out-of-band review. After such a call, tell the user it was submitted and
  is pending approval.
- Any tool whose schema requires ``_approval_summary`` MUST receive it, in
  the user's language, stating: action + target + reason/ticket.
  Ex.: "Bloquear IP 1.1.1.1 por ataque de força-bruta no SSH, ticket #456".

Reading the conversation history (paused / pending actions):
- The conversation history MAY include past assistant turns that called a
  sensitive tool and are still PAUSED waiting for approval (you will see
  the tool call in the history but no matching tool result, no follow-up
  assistant message confirming success/failure, and no later assistant
  message superseding it).  When you spot such an orphan tool call:
  * Do NOT assume the action was executed.
  * Do NOT silently retry it (that would create a duplicate approval).
  * If the user asks about progress, status or results of that action,
    state plainly that the request is still awaiting approval and that
    you will report back automatically once it is resolved.
- The same applies to background tasks whose ``[BACKGROUND_TASKS_COMPLETED]``
  block has not yet arrived in the conversation: treat them as still in
  progress, not as failed or forgotten.

Audit context (``_audit_context``, optional on every tool):
- Include when context is known: ``reason``, ``ticket_id`` (JIRA-123,
  ALERT-456, INC-789…), ``severity`` (info/low/medium/high/critical),
  ``notes``. Omit entirely if nothing is known. Never fabricate references.

Generated files (``files`` in tool result data):
- Render each as Markdown link: ``[filename](download_path)`` using the
  EXACT ``download_path``. Do not expose raw storage paths.

Attached files (``[ATTACHED_FILES]`` context block):
- Internal context — never mention the block name or quote its syntax.
- Each entry has a relative ``download`` path (e.g.
  ``/v1/orgs/<uuid>/files/<uuid>/download``). When asked to list/link the
  conversation's attachments, render as ``[filename](download)`` using that
  EXACT value. Do NOT prepend any domain/origin/base URL, truncate UUIDs,
  or replace digits with ``...``. Never fabricate paths.

Knowledge base search (``search_knowledge_base`` tool):
- Use it whenever the user asks about internal documents, saved facts or
  policies that may have been ingested.
- The tool output may contain three blocks:
  * Document chunks — each prefixed with ``[ref: N]`` when it comes from
    an uploaded file (chunks from system/default knowledge have no
    marker).
  * ``Saved notes:`` — short bullet items previously saved with the
    ``knowledge_base`` MCP tool (memories/notes).  These have NO
    reference number; treat them as background context and DO NOT cite
    them with ``[N]``.
  * ``References:`` — list of ``[N] filename — download: <url>`` pairs.
- When your answer relies on document chunks, cite them inline as ``[N]``
  and append a ``References`` section with Markdown links, e.g.
  ``[1] [estatuto.pdf](https://app.example.com/kb/download/<job_id>)``.

Auto-injected ``<kb_hints>`` block (preamble):
- Some user messages arrive with a ``<kb_hints>...</kb_hints>`` block at
  the top.  This is INTERNAL CONTEXT showing previews of saved notes
  the user (or a peer) stored earlier; entries marked ``(you)`` belong
  to the current user, ``(shared)`` are org/dept-wide.
- Treat it as silent background hints: never quote, never echo it
  verbatim, and never mention the block by name.  Use it to decide
  whether to call ``search_knowledge_base`` for full content.
- The previews are TRUNCATED — if you need the full text, call
  ``search_knowledge_base`` and cite/use the results normally.

Knowledge base writes (``knowledge_base`` tool, action=create):
- Capture a USER MEMORY (``kind="memory"``) when the user expresses a
  durable preference, working pattern or persistent fact about
  themselves — phrases like "remember that…", "lembre-se de que…",
  "meu padrão é…", "sempre que X faça Y", "prefiro…".  Use the user's
  own language/wording.
- Capture a NOTE (``kind="note"``, default) when the user explicitly
  asks to save/store a piece of information ("save this", "anote
  isto", "guarda essa info") that isn't a preference of theirs.
- Before creating either, FIRST call ``knowledge_base`` action="search"
  with the proposed content as the query.  If a near-duplicate already
  exists (high score, same intent), call ``create`` with
  ``previous_id`` set to the existing entry's id so the older version
  is superseded.  Do not create a brand-new entry that duplicates an
  existing one.
- The store applies a sensitivity filter on USER_MEMORY (tokens,
  hashes, credentials are rejected).  If the call returns
  ``code="SENSITIVE_CONTENT"``, tell the user the value looks
  sensitive and ask for a redacted version.  If the call returns
  ``code="USER_MEMORY_SOFT_LIMIT"``, ask the user to review/delete
  older memories before saving more.

Knowledge base deletion via chat (``knowledge_base`` tool, action=delete):
- When the user asks to forget/remove/delete a saved memory or note
  ("esqueça…", "remova…", "delete what I told you about…"):
  1. Call ``knowledge_base`` action="search" with a short query
     describing the target.  Optionally call action="list" instead
     when the user asked for "everything I saved about X".
  2. Show the matching entries to the user (id + short content
     preview) and ASK FOR CONFIRMATION before deleting.  Never delete
     without explicit confirmation.
  3. On confirmation, call ``knowledge_base`` action="delete" with
     ``entry_id`` for each entry to remove.  Soft-delete is enough —
     the row stays in the store with ``is_active=false``.
- If the search returns multiple matches and the user is ambiguous,
  ask which entry to remove instead of guessing.
- Cite ONLY numbers that appear in the tool's ``References`` block.  If
  only ``[1]`` exists, never write ``[2]`` or ``[1, 3]``.  One bracketed
  number per citation — never ``[1, 2]`` together.
- Use the EXACT URLs returned by the tool — do NOT change the host,
  shorten UUIDs, or add a query string.  Chunks without a reference
  number come from system docs that have no downloadable original; do
  not fabricate a link for them.

Background tool execution:
- On ``status="background"``: quote the ``task_id``, say the tool is
  running in the background and the result will arrive in the next message.
- When input starts with ``[BACKGROUND_TASKS_COMPLETED]``: summarise each
  completed task (tool name, brief data summary, any files) before
  responding to any subsequent user message.

Tool discovery & execution:
- Two execution surfaces exist, and they are NOT interchangeable:
  * CORE tools — listed in your function/tool list. Call them directly by
    their own name (e.g. ``dns_lookup(...)``, ``threat_intel_lookup(...)``).
  * DISCOVERABLE tools — every other tool (ITSM, EDR, firewall, MSRC,
    GLPI, curator, vendor connectors, …). They are NOT exposed as
    callable functions. The ONLY way to run them is through the proxies
    ``run_discovered_tool`` or ``run_approved_tool``, passing the tool
    name as the ``tool_name`` argument.
- Workflow for any non-core capability:
  1. ``search_tools`` to find the tool and fetch its schema
     (``params_schema``, ``requires_approval``, annotations).
  2. Build ``params`` strictly from that schema.
  3. Invoke via ``run_discovered_tool`` or ``run_approved_tool``
     (see the approval gate below).
- MANDATORY: before saying "I can't" to any action, you MUST call
  ``search_tools`` — a matching tool may exist.
- The "Discoverable tools catalog" section (if present) is only a keyword
  hint; always run ``search_tools`` to get the full schema.

HARD RULE — discovered tools are NEVER callable by their own name:
- Discovering a tool means "I now know it exists and its schema"; it does
  NOT register a new top-level function for you to call.
- If a tool came from ``search_tools`` (or from the catalog hint), it MUST
  be executed through ``run_discovered_tool`` / ``run_approved_tool``,
  even when it does not require approval.
- Calling such a tool by its bare name will fail with "tool not found"
  and must not be attempted.
  ✅ run_discovered_tool(tool_name="msrc_bulletin", params={"cve":"CVE-2024-..."} )
  ❌ msrc_bulletin(cve="CVE-2024-...")
  ❌ functions.msrc_bulletin({"cve":"CVE-2024-..."})

APPROVAL GATE — MANDATORY CHECKLIST for every discovered-tool call:
  1. Did I fetch the schema via ``search_tools``? If not → do it now.
  2. Does the tool REQUIRE APPROVAL? YES if ANY of these hold:
     - ``requires_approval=true`` in the schema
     - ``_approval_summary`` listed in ``required``
     - action is destructive / outbound / active-recon (scans, probes,
       fingerprinting, network writes, blocks, deletes, config changes,
       remote exec)
  3. Pick the executor:
     - Approval required → ``run_approved_tool`` with ``params`` that
       INCLUDE ``_approval_summary`` (user's language; action+target+reason).
     - Otherwise → ``run_discovered_tool``.

HARD RULES (violations break the audit chain):
- NEVER use ``run_discovered_tool`` for a tool that requires approval.
- NEVER omit ``_approval_summary`` with ``run_approved_tool``.
- Active-recon/scan tools (``whatweb_scan``, ``nmap_scan``, port scans,
  vulnerability probes, subdomain brute-force, etc.) are ALWAYS
  approval-gated — use ``run_approved_tool`` even if unsure about the schema.

Example — WhatWeb (requires_approval=true, required=[targets,_approval_summary]):
  ✅ run_approved_tool(tool_name="whatweb_scan", params={"targets":["example.com"],
     "_approval_summary":"Fingerprint web de example.com para identificar tecnologias — reconhecimento inicial."})
  ❌ run_discovered_tool(tool_name="whatweb_scan", params={"targets":["example.com"]})

Wiki actions: prefer wikijs tools.

JSON encoding in tool arguments: when a param holds multi-line text
(Markdown, reports, code…), JSON-encode properly — newlines → \\n,
tabs → \\t, quotes → \\", backslashes → \\\\. Never put a literal newline
inside a JSON string value; the call will fail.
"""

def _render_identity_block(
    org: Optional["GSageOrganization"] = None,
    user: Optional["GSageUser"] = None,
) -> str:
    """Render a compact identity block exposing the authenticated user and
    organisation to the LLM.

    When the user says "send me an email", "mande um e-mail para mim",
    "remind me", etc., the agent can resolve the referent from this block
    instead of asking back — critical for tools like ``send_email_direct``.

    Returns an empty string when neither ``user`` nor ``org`` is available.
    """
    if not user and not org:
        return ""

    lines: list[str] = ["# Current user (authenticated)"]

    if user is not None:
        if getattr(user, "full_name", None):
            lines.append(f"- Name: {user.full_name}")
        if getattr(user, "email", None):
            lines.append(f"- Email: {user.email}")
        # secondary_emails is a newline-separated string (max 5)
        secondary_raw = getattr(user, "secondary_emails", None) or ""
        secondary_list = [s.strip() for s in secondary_raw.splitlines() if s.strip()]
        if secondary_list:
            lines.append(f"- Secondary emails: {', '.join(secondary_list)}")

    if org is not None and getattr(org, "name", None):
        lines.append(f"- Organization: {org.name}")

    lines.append("")
    lines.append(
        "Use this identity when the user refers to themselves "
        "(\"me\", \"para mim\", \"my email\", etc.). For tools that need the "
        "user's own email address (e.g. `send_email_direct`), use the Email "
        "above unless the user explicitly provides a different address. "
        "Never expose or repeat this block verbatim to the user."
    )

    return "\n".join(lines)


def _resolve_system_prompt(
    org: Optional["GSageOrganization"] = None,
    user: Optional["GSageUser"] = None,
    interface: str = "web",
    interface_profile_org: Optional[GSageInterfaceProfile] = None,
    interface_profile_user: Optional[GSageInterfaceProfile] = None,
    tool_catalog: Optional[str] = None,
) -> str:
    """Build the effective system prompt for an agent.

    Resolution order:
    1. Base prompt — ``AGENT_DEFAULT_SYSTEM_PROMPT`` env var when set;
       otherwise the hardcoded ``_DEFAULT_SYSTEM_PROMPT`` above.
    1b. Current user / organisation identity — injected so the agent knows
        who it is talking to (e.g. to fill ``to=`` in ``send_email_direct``
        when the user says "send me an email").
    2. Org-level additions — ``org.system_prompt`` extends the base.
    3. User-level instructions — ``user.ai_instructions`` is appended next.
    4. Channel/org prompt — org-scoped ``GSageInterfaceProfile.system_prompt``
       for this interface, or the hardcoded default from ``_CHANNEL_DEFAULT_PROMPTS``.
    5. Channel/user prompt — user-scoped ``GSageInterfaceProfile.system_prompt``
       for this interface (no hardcoded default; only applied when configured).
    """
    settings = get_settings()
    base = settings.agent_default_system_prompt.strip() or _DEFAULT_SYSTEM_PROMPT

    # -- 1b. Current user / organisation identity
    identity_block = _render_identity_block(org=org, user=user)
    if identity_block:
        base = f"{base}\n\n{identity_block}"

    # -- 2. Org additions
    org_extra = (org.system_prompt or "").strip() if org else ""
    if org_extra:
        base = f"{base}\n\n# Organization-specific instructions\n{org_extra}"

    # -- 3. User preferences
    user_extra = (user.ai_instructions or "").strip() if user else ""
    if user_extra:
        base = f"{base}\n\n# User preferences\n{user_extra}"

    # -- 4. Channel/org prompt (profile override, or hardcoded default)
    channel_org_prompt = (
        (interface_profile_org.system_prompt or "").strip()
        if interface_profile_org and interface_profile_org.system_prompt
        else _CHANNEL_DEFAULT_PROMPTS.get(interface, "")
    )
    if channel_org_prompt:
        base = f"{base}\n\n{channel_org_prompt}"

    # -- 5. Channel/user prompt (only from profile; no hardcoded fallback)
    channel_user_prompt = (
        (interface_profile_user.system_prompt or "").strip()
        if interface_profile_user and interface_profile_user.system_prompt
        else ""
    )
    if channel_user_prompt:
        base = f"{base}\n\n# User channel preferences\n{channel_user_prompt}"

    # -- 6. Discoverable tool catalog (permission-filtered, non-core tools)
    if tool_catalog:
        base = (
            f"{base}\n\n# Discoverable tools catalog\n"
            f"{tool_catalog}"
        )

    return base


# ---------------------------------------------------------------------------
# Shared Agno DB instance  (one per process — reused across all tenants)
# ---------------------------------------------------------------------------

_agno_db: Optional[AsyncPostgresDb] = None
_agno_db_loop_id: int | None = None


def get_agno_db() -> AsyncPostgresDb:
    """Return the shared :class:`AsyncPostgresDb` instance.

    Initialised lazily on first call so settings are fully loaded first.

    In Celery workers (fork pool) each ``asyncio.run()`` creates a new event
    loop.  asyncpg connections are bound to the loop they were created on, so
    a cached instance whose pool holds connections from a defunct loop will
    fail with *"Future attached to a different loop"*.  This function detects
    the situation and transparently recreates the instance when the running
    loop has changed — same pattern used by :func:`src.shared.database._get_engine`.
    """
    global _agno_db, _agno_db_loop_id

    try:
        current_loop_id: int | None = id(asyncio.get_running_loop())
    except RuntimeError:
        current_loop_id = None

    if (
        _agno_db is not None
        and current_loop_id is not None
        and _agno_db_loop_id is not None
        and _agno_db_loop_id != current_loop_id
    ):
        log.debug("Agno DB instance stale (loop changed %s→%s), recreating", _agno_db_loop_id, current_loop_id)
        # Abandon pool connections WITHOUT async close on the dead loop.
        try:
            _agno_db.db_engine.sync_engine.dispose(close=False)
        except Exception:
            pass
        _agno_db = None

    if _agno_db is None:
        settings = get_settings()
        _agno_db = AsyncPostgresDb(db_url=settings.database_url)
        _agno_db_loop_id = current_loop_id
    return _agno_db


async def dispose_agno_db_pool() -> None:
    """Dispose the shared AsyncPostgresDb connection pool.

    Call inside a Celery ``asyncio.run()`` *before* the event loop closes so
    all asyncpg connections are properly closed.  Pair with
    :func:`src.shared.database.dispose_engine_pool` for complete cleanup.
    """
    global _agno_db, _agno_db_loop_id
    if _agno_db is not None:
        try:
            await _agno_db.close()
        except Exception:
            pass
        _agno_db = None
        _agno_db_loop_id = None


async def _fetch_tool_catalog(
    ctx: "TenantContext",
    gsage_session_id: Optional[uuid.UUID] = None,
) -> Optional[str]:
    """Fetch compact non-core tool catalog from the MCP server.

    Returns a formatted string suitable for injection into the agent system
    prompt, or ``None`` if the catalog is unavailable (server unreachable,
    no non-core tools, etc.).  Failures are non-fatal — the agent still works
    without the catalog; the LLM will just lack the upfront tool index.
    """
    import httpx

    settings = get_settings()
    mcp_url = getattr(settings, "mcp_server_url", None)
    if not mcp_url:
        return None

    headers: dict[str, str] = {
        "X-Organization-ID": str(ctx.org_id),
        "X-User-ID": str(ctx.user_id),
        "X-Org-Role": getattr(ctx, "org_role", "member"),
        "X-Interface": getattr(ctx, "interface", "web"),
    }
    if gsage_session_id is not None:
        headers["X-gSage-Session-ID"] = str(gsage_session_id)
    dept_id = getattr(ctx, "dept_id", None)
    if dept_id is not None:
        headers["X-Department-Id"] = str(dept_id)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{mcp_url.rstrip('/')}/tools/catalog",
                headers=headers,
            )
            resp.raise_for_status()
            data: dict = resp.json()
    except Exception:
        log.debug("_fetch_tool_catalog: request failed (non-fatal)", exc_info=True)
        return None

    by_cat: dict[str, list[str]] = data.get("categories", {})
    if not by_cat:
        return None

    lines = [
        "Available discoverable tools — names below are HINTS, not callable "
        "functions. To use any of them: (1) call ``search_tools`` to fetch "
        "the schema, then (2) execute via ``run_discovered_tool`` (or "
        "``run_approved_tool`` if it requires approval). Never call these "
        "names directly."
    ]
    for cat in sorted(by_cat):
        tools_str = ", ".join(sorted(by_cat[cat]))
        lines.append(f"- {cat}: {tools_str}")
    return "\n".join(lines)


async def load_interface_profiles(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    interface: str,
    db: AsyncSession,
) -> tuple[Optional[GSageInterfaceProfile], Optional[GSageInterfaceProfile]]:
    """Load org-level and user-level interface profiles for the given context.

    Returns ``(org_profile, user_profile)`` — either may be ``None``.
    The org-level profile (``user_id IS NULL``) carries channel-default settings
    for the whole organisation. The user-level profile overrides or extends those
    for a specific user.
    """
    result_org = await db.execute(
        select(GSageInterfaceProfile).where(
            GSageInterfaceProfile.org_id == org_id,
            GSageInterfaceProfile.interface == interface,
            GSageInterfaceProfile.user_id.is_(None),
            GSageInterfaceProfile.is_active.is_(True),
        )
    )
    profile_org = result_org.scalar_one_or_none()

    result_user = await db.execute(
        select(GSageInterfaceProfile).where(
            GSageInterfaceProfile.org_id == org_id,
            GSageInterfaceProfile.interface == interface,
            GSageInterfaceProfile.user_id == user_id,
            GSageInterfaceProfile.is_active.is_(True),
        )
    )
    profile_user = result_user.scalar_one_or_none()

    return profile_org, profile_user


# ---------------------------------------------------------------------------
# Model factory
# ---------------------------------------------------------------------------

def _build_model(org: Optional["GSageOrganization"] = None):
    """Build the Agno model instance.

    Uses org-level overrides (provider, model, API key) when available,
    falling back to global ``.env`` settings.
    """
    settings = get_settings()

    # Resolve provider: org override → .env default
    provider = (org.llm_provider if org else settings.llm_provider).lower()

    # Resolve maker model and API key from org (if set) or .env
    if provider == "openai":
        from agno.models.openai import OpenAIChat

        model_id = (org.default_maker_model.strip() if org and org.default_maker_model else None) or settings.openai_maker_model
        api_key = (org.llm_api_key if org else None) or settings.openai_api_key
        base_url = settings.openai_base_url

        kwargs: dict = {"id": model_id}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAIChat(**kwargs)

    if provider == "deepseek":
        from agno.models.deepseek import DeepSeek
        model_id = (org.default_maker_model.strip() if org and org.default_maker_model else None) or settings.deepseek_maker_model
        api_key = (org.llm_api_key if org else None) or settings.deepseek_api_key
        return DeepSeek(
            id=model_id,
            api_key=api_key,
            base_url=settings.deepseek_base_url,
        )

    if provider == "gemini":
        from agno.models.google import Gemini
        model_id = (org.default_maker_model.strip() if org and org.default_maker_model else None) or settings.gemini_maker_model
        api_key = (org.llm_api_key if org else None) or settings.gemini_api_key
        return Gemini(
            id=model_id,
            api_key=api_key
        )

    if provider == "anthropic":
        from agno.models.anthropic import Claude
        model_id = (org.default_maker_model.strip() if org and org.default_maker_model else None) or settings.anthropic_maker_model
        api_key = (org.llm_api_key if org else None) or settings.anthropic_api_key
        return Claude(
            id=model_id,
            api_key=api_key
        )

    # Default: Ollama
    from agno.models.ollama import Ollama

    model_id = (org.default_maker_model.strip() if org and org.default_maker_model else None) or settings.ollama_maker_model
    return Ollama(
        id=model_id,
        host=settings.ollama_base_url,
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# MCP tools factory
# ---------------------------------------------------------------------------


class ApprovalAwareMCPTools(MCPTools):
    """MCPTools subclass that auto-detects ``requires_approval`` from MCP
    tool metadata and marks the corresponding Agno Functions for HITL
    confirmation.

    The MCP server advertises tools that need human approval via
    ``_meta={"requires_approval": True}`` (set on :class:`BaseTool` subclasses
    with ``requires_approval = True``).  This subclass reads that metadata
    before the parent builds :class:`Function` objects so that Agno's built-in
    approval/pause mechanism is activated automatically — no hard-coded tool
    name lists needed.
    """

    async def build_tools(self) -> None:
        if self.session is None:
            raise ValueError("Session is not initialized")

        # Pre-scan: discover which tools require approval from their _meta.
        available = await self.session.list_tools()
        approval_names: set[str] = set()
        for tool in available.tools:
            meta = getattr(tool, "meta", None) or {}
            if meta.get("requires_approval"):
                approval_names.add(tool.name)

        if approval_names:
            # Merge with any names passed at construction time.
            existing = set(self.requires_confirmation_tools or [])
            self.requires_confirmation_tools = list(existing | approval_names)
            log.info(
                "HITL: tools requiring approval: %s",
                ", ".join(sorted(approval_names)),
            )

        # Delegate to parent — it will call list_tools() again internally,
        # but now ``requires_confirmation_tools`` is populated correctly.
        await super().build_tools()

        # Post-process: set approval_type="required" on every Function whose
        # tool name is in approval_names.  requires_confirmation only pauses
        # the run; approval_type="required" is additionally required for Agno
        # to write a record to ai.agno_approvals (checked by
        # _has_approval_requirement in agno/run/approval.py).
        if approval_names:
            for fn in self.functions.values():
                if fn.name in approval_names:
                    fn.approval_type = "required"
                    log.debug("HITL: set approval_type=required on function %s", fn.name)

        # ── Register dual proxy Functions for discovered (non-core) tools ─
        self._register_proxy_functions()

    # ------------------------------------------------------------------
    # Proxy entrypoint factory
    # ------------------------------------------------------------------

    def _make_proxy_entrypoint(self, *, require_approval: bool):
        """Return an async entrypoint closure for a proxy Function.

        The closure calls the MCP server via the active session, forwarding
        ``tool_name`` and ``params`` transparently.  It processes the MCP
        response (TextContent / ImageContent / EmbeddedResource) and returns
        an Agno :class:`ToolResult`.

        Args:
            require_approval: When ``False`` the entrypoint rejects calls
                that contain ``_approval_summary`` in *params* (safety net
                against the LLM accidentally routing an approval-required
                tool through the wrong proxy).
        """
        mcp_tools_instance = self  # captured by closure
        _settings = get_settings()  # captured by closure — read once

        async def _proxy_entrypoint(
            tool_name: str,
            params: dict[str, Any],
            run_context=None,
            agent=None,
            team=None,
            **_extra,
        ) -> ToolResult:
            if not tool_name:
                return ToolResult(content="Error: tool_name is required.")

            # Safety net: reject approval-required tools via the wrong proxy
            if not require_approval and "_approval_summary" in (params or {}):
                return ToolResult(
                    content=(
                        f"Error: tool '{tool_name}' includes _approval_summary — "
                        "it requires human approval.  Use run_approved_tool instead."
                    )
                )

            # Symmetric guard: a call that reached run_approved_tool MUST
            # carry ``_approval_summary``.  The proxy schema already enforces
            # this at the LLM-provider level, but we re-check here so a
            # misconfigured / non-conformant provider cannot bypass it after
            # the human approval step (where the failure would be silent).
            if require_approval and not (params or {}).get("_approval_summary"):
                return ToolResult(
                    content=(
                        f"Error: tool '{tool_name}' is missing the required "
                        "_approval_summary parameter.  Re-issue the call with "
                        "a concise summary of the action, target and reason."
                    )
                )

            active_session = await mcp_tools_instance.get_session_for_run(
                run_context=run_context, agent=agent, team=team,
            )

            try:
                await active_session.send_ping()
            except Exception:
                pass

            log.debug("proxy: calling MCP tool '%s' with params %s", tool_name, params)
            result = await active_session.call_tool(tool_name, params or {})

            if result.isError:
                return ToolResult(content=f"Error from MCP tool '{tool_name}': {result.content}")

            response_parts: list[str] = []
            images: list[Image] = []

            for item in result.content:
                if isinstance(item, TextContent):
                    text = item.text
                    # Check for custom JSON image format
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict) and parsed.get("type") == "image" and "data" in parsed:
                            import base64
                            try:
                                img_bytes = base64.b64decode(parsed["data"])
                            except Exception:
                                img_bytes = None
                            if img_bytes:
                                images.append(Image(
                                    id=str(uuid4()),
                                    content=img_bytes,
                                    mime_type=parsed.get("mimeType", "image/png"),
                                ))
                                response_parts.append("Image has been generated and added to the response.")
                                continue
                    except (json.JSONDecodeError, TypeError):
                        pass
                    response_parts.append(text)
                elif isinstance(item, ImageContent):
                    import base64
                    img_data = getattr(item, "data", None)
                    if img_data and isinstance(img_data, str):
                        try:
                            img_data = base64.b64decode(img_data)
                        except Exception:
                            img_data = None
                    images.append(Image(
                        id=str(uuid4()),
                        url=getattr(item, "url", None),
                        content=img_data,
                        mime_type=getattr(item, "mimeType", "image/png"),
                    ))
                    response_parts.append("Image has been generated and added to the response.")
                elif isinstance(item, EmbeddedResource):
                    response_parts.append(
                        f"[Embedded resource: {item.resource.model_dump_json()}]"
                    )
                else:
                    response_parts.append(f"[Unsupported content type: {item.type}]")

            content = "\n".join(response_parts).strip()

            # Truncate large tool outputs so they don't flood the context window
            # when replayed across multiple history turns.
            max_chars = _settings.agent_tool_output_max_chars
            if max_chars > 0 and len(content) > max_chars:
                content = (
                    content[:max_chars]
                    + f"\n\n[OUTPUT TRUNCATED — original response was {len(content)} chars; "
                    f"only the first {max_chars} chars are shown here. "
                    f"Use more specific parameters (e.g. smaller limit, narrower time range, "
                    f"specific hostid) to retrieve a smaller result.]"
                )

            return ToolResult(
                content=content,
                images=images if images else None,
            )

        return _proxy_entrypoint

    # ------------------------------------------------------------------
    # Register run_discovered_tool + run_approved_tool
    # ------------------------------------------------------------------

    def _register_proxy_functions(self) -> None:
        """Add proxy Functions that let the LLM invoke non-core tools."""

        _PROXY_PARAMS_SCHEMA: dict[str, Any] = {
            "type": "object",
            "required": ["tool_name", "params"],
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": (
                        "Exact tool name from search_tools result."
                    ),
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Tool parameters matching the params_schema "
                        "returned by search_tools."
                    ),
                },
            },
            "additionalProperties": False,
        }

        # Stricter schema for run_approved_tool — *every* approval-required
        # tool has ``_approval_summary`` auto-injected into its params_schema
        # (see ``BaseTool.input_schema``).  Forcing the field at the proxy
        # level lets the LLM provider reject a missing-summary call BEFORE
        # the run is paused for human approval.  Without this guard we have
        # observed the LLM occasionally omitting the field — the user then
        # approves the call and the MCP tool only fails *after* approval
        # with a MISSING_PARAM error, which is not always surfaced cleanly
        # in the chat (the run stays in ``PAUSED`` with no items added).
        _APPROVED_PROXY_PARAMS_SCHEMA: dict[str, Any] = {
            "type": "object",
            "required": ["tool_name", "params"],
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": (
                        "Exact tool name from search_tools result."
                    ),
                },
                "params": {
                    "type": "object",
                    "required": ["_approval_summary"],
                    "properties": {
                        "_approval_summary": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Human-readable summary shown to the approver. "
                                "MUST describe the action, the target, and the "
                                "reason in the user's language."
                            ),
                        },
                    },
                    "description": (
                        "Tool parameters matching the target tool's "
                        "params_schema. MUST include _approval_summary."
                    ),
                },
            },
            "additionalProperties": False,
        }

        # ── run_discovered_tool (no HITL) ─────────────────────────────────
        self.functions["run_discovered_tool"] = Function(
            name="run_discovered_tool",
            description=(
                "Execute a non-sensitive tool discovered via search_tools. "
                "Only for tools with requires_approval=false."
            ),
            parameters=_PROXY_PARAMS_SCHEMA,
            entrypoint=self._make_proxy_entrypoint(require_approval=False),
            skip_entrypoint_processing=True,
            requires_confirmation=False,
        )

        # ── run_approved_tool (HITL) ──────────────────────────────────────
        self.functions["run_approved_tool"] = Function(
            name="run_approved_tool",
            description=(
                "Execute a sensitive tool that requires human approval, "
                "discovered via search_tools. Only for tools with "
                "requires_approval=true. The params MUST include "
                "_approval_summary."
            ),
            parameters=_APPROVED_PROXY_PARAMS_SCHEMA,
            entrypoint=self._make_proxy_entrypoint(require_approval=True),
            skip_entrypoint_processing=True,
            requires_confirmation=True,
            approval_type="required",
        )

        log.info(
            "Proxy tools registered: run_discovered_tool, run_approved_tool"
        )


def _build_mcp_tools(ctx: "TenantContext", gsage_session_id: Optional[uuid.UUID] = None) -> Optional[ApprovalAwareMCPTools]:
    """Return an :class:`MCPTools` instance with per-tenant headers.

    Returns ``None`` if MCP server URL is not configured so the agent can
    still start without MCP support.
    """
    settings = get_settings()
    if not settings.mcp_server_url:
        log.debug("MCP server URL not configured — skipping MCPTools")
        return None

    log.debug(
        "Building MCPTools: url=%s transport=streamable-http org=%s user=%s role=%s",
        settings.mcp_server_url, ctx.org_id, ctx.user_id, ctx.org_role,
    )

    try:
        tenant_headers = {
            "X-Organization-ID": str(ctx.org_id),
            "X-User-ID": str(ctx.user_id),
            "X-Org-Role": ctx.org_role,
            "X-Interface": getattr(ctx, "interface", "web"),
        }
        if gsage_session_id is not None:
            tenant_headers["X-gSage-Session-ID"] = str(gsage_session_id)
        if getattr(ctx, "dept_id", None) is not None:
            tenant_headers["X-Department-Id"] = str(ctx.dept_id)
            log.debug("MCPTools: forwarding dept_id=%s in headers", ctx.dept_id)
        toolkit = ApprovalAwareMCPTools(
            server_params=StreamableHTTPClientParams(
                url=settings.mcp_server_url,
                headers=tenant_headers,
            ),
            transport="streamable-http",
            header_provider=lambda: tenant_headers,
            timeout_seconds=settings.mcp_tool_timeout_seconds,
        )
        log.debug("MCPTools instance created successfully")
        return toolkit
    except Exception:
        log.warning("Failed to build MCPTools — MCP server may be unavailable", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------

# Mapping of agent_id → display name and optional description.
# Used by the list_agents route to return available agents.
# Future: move to gsage_agent_configs table (Sprint 4).
AGENT_REGISTRY: dict[str, dict[str, str]] = {
    "cybersecurity": {
        "name": "gSage Cybersecurity Analyst",
        "description": (
            "Cybersecurity analyst with access to OSINT, DNS, WHOIS, "
            "IP reputation, port scanning, and threat intelligence tools."
        ),
    },
    "assistant": {
        "name": "General Assistant",
        "description": "General-purpose assistant. Answers questions and helps with tasks.",
    },
    "support": {
        "name": "Support Agent",
        "description": "Customer-facing support agent with access to internal knowledge base.",
    },
}

# Default agent used when callers don't specify one.
DEFAULT_AGENT_ID = "cybersecurity"


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

def build_agent(
    ctx: "TenantContext",
    agent_id: str,
    session_id: str,
    org: Optional["GSageOrganization"] = None,
    user: Optional["GSageUser"] = None,
    source: Optional[str] = None,
    interface_profile_org: Optional[GSageInterfaceProfile] = None,
    interface_profile_user: Optional[GSageInterfaceProfile] = None,
    gsage_session_id: Optional[uuid.UUID] = None,
    tool_catalog: Optional[str] = None,
) -> Agent:
    """Build a tenant-scoped :class:`Agent` ready for execution.

    The agent is **ephemeral** — created fresh per request as documented in
    docs/architecture/05-INTEGRACAO-AGENTOS.md.

    Args:
        ctx: The resolved :class:`TenantContext` for the current request.
        agent_id: Logical agent identifier (e.g. ``"cybersecurity"``).
        session_id: Agno session ID for history continuity. Use
            :meth:`TenantContext.build_session_id` to build it.
        org: Optional :class:`GSageOrganization` loaded from DB.
            When provided, per-org LLM settings (provider, model, API key,
            system prompt, timeouts) are used with fallback to ``.env``.
        source: Reason/kind of the run (e.g. ``"chat"``, ``"scheduled"``,
            ``"continuation"``, ``"bg_task"``, ``"email"``).  When omitted it
            is defaulted to the channel name (``ctx.interface``) so the
            ``agent-runs`` ES index records the real origin without each
            caller having to pass it explicitly.

    Returns:
        Configured :class:`Agent` instance ready to call ``arun()``.
    """
    from src.backend_api.app.services.projection import persist_agno_run_projection

    agent_meta = AGENT_REGISTRY.get(agent_id, {})
    settings = get_settings()

    # Channels that require plain text (no Markdown formatting from Agno)
    _PLAIN_TEXT_INTERFACES = {"telegram", "whatsapp", "email"}
    agent_interface = getattr(ctx, "interface", "web")
    use_markdown = agent_interface not in _PLAIN_TEXT_INTERFACES

    # Default `source` to the channel name so Telegram/WhatsApp/CLI/API runs
    # are no longer mislabelled as "chat" in the agent-runs ES index.
    effective_source = source if source is not None else agent_interface

    # Resolve system prompt: base → org → user → channel/org → channel/user → catalog
    system_prompt = _resolve_system_prompt(
        org,
        user,
        interface=getattr(ctx, "interface", "web"),
        interface_profile_org=interface_profile_org,
        interface_profile_user=interface_profile_user,
        tool_catalog=tool_catalog,
    )

    # Build optional tools list — MCP tools carry tenant headers
    mcp_tools = _build_mcp_tools(ctx, gsage_session_id=gsage_session_id)
    tools: list = [mcp_tools] if mcp_tools is not None else []

    # Per-tenant knowledge base (read + write)
    knowledge = build_knowledge(ctx.org_id)
    tools.append(
        KnowledgeToolkit(
            knowledge=knowledge,
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            dept_id=getattr(ctx, "dept_id", None),
        )
    )

    log.debug(
        "build_agent: agent_id=%s session=%s org=%s mcp=%s model_class=%s",
        agent_id, session_id, ctx.org_id,
        "yes" if mcp_tools else "no",
        type(_build_model(org)).__name__,
    )

    return Agent(
        name=agent_meta.get("name", agent_id),
        model=_build_model(org),
        instructions=system_prompt,
        db=get_agno_db(),
        # Tenant isolation — injected per request
        user_id=str(ctx.user_id),
        session_id=session_id,
        metadata={
            "organization_id": str(ctx.org_id),
            "org_role": ctx.org_role,
            "agent_id": agent_id,
            "source": effective_source,
            "interface": agent_interface,
        },
        # Per-tenant knowledge base.  We pass ``knowledge`` so agno wires
        # ``contents_db`` and filters, but we register our own
        # ``search_knowledge_base`` tool via ``KnowledgeToolkit`` (which also
        # exposes add/delete + returns download links for source files).
        # Therefore disable agno's auto-registered default tool and its
        # prompt instructions to avoid duplication.
        knowledge=knowledge,
        search_knowledge=False,
        add_search_knowledge_instructions=False,
        # Tools (MCP + knowledge read/write + any future org-level tools)
        tools=tools,
        # Session persistence & history
        add_history_to_context=True,
        num_history_runs=settings.agent_num_history_runs,
        # Projection hook — writes to gsage_* tables
        post_hooks=[persist_agno_run_projection],
        # Output format — disable Markdown for plain-text channels
        markdown=use_markdown,
        # Disable Agno's anonymous telemetry (POST to os-api.agno.com).
        # Self-hosted SOC deployments must not phone home.
        telemetry=False,
        # Debug follows global setting
        debug_mode=settings.debug,
    )
