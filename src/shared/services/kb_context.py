"""gSage AI — Knowledge-base preamble builder for chat turns.

Auto-injection of a short ``<kb_hints>`` block at the beginning of every
LLM turn so the model is reminded of the user's saved notes/memories
without having to call ``search_knowledge_base`` first.

The lookup queries the shared ``KnowledgeBase`` Weaviate collection (used
by the MCP ``knowledge_base`` CRUD tool) — uploaded documents in agno's
per-tenant collection are NOT queried here; chunked documents stay
behind the explicit tool call to keep the preamble compact.

Behaviour is fully governed by ``settings.kb_auto_inject_*``; failures
are absorbed silently (auto-injection must never break the chat turn).
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from src.shared.config.settings import get_settings
from src.shared.security.context import AgentContext, RequestSource
from src.shared.services.knowledge_service import KnowledgeService

logger = logging.getLogger(__name__)

_PREAMBLE_OPEN = "<kb_hints>"
_PREAMBLE_CLOSE = "</kb_hints>"


def _truncate(text: str, max_chars: int) -> str:
    text = " ".join((text or "").split())  # collapse whitespace
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _make_lookup_context(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    dept_id: Optional[uuid.UUID],
) -> AgentContext:
    """Build a minimal :class:`AgentContext` for ``KnowledgeService`` calls.

    ``KnowledgeService`` only reads ``org_id`` / ``user_id`` / ``dept_id``
    from the context; permissions and request_id are unused by search,
    so a placeholder is sufficient.
    """
    return AgentContext(
        org_id=org_id,
        user_id=user_id,
        group_ids=[],
        permissions=["crud:knowledge_base:read"],
        request_id=uuid.uuid4(),
        source=RequestSource.WEB,
        dept_id=dept_id,
    )


async def build_turn_preamble(
    user_message: str,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    dept_id: Optional[uuid.UUID] = None,
) -> str:
    """Return a ``<kb_hints>...</kb_hints>`` block for *user_message* or "".

    The block lists up to ``kb_auto_inject_user_top_n`` user-private
    notes followed by up to ``kb_auto_inject_shared_top_n`` org-wide
    notes, each truncated to ``kb_auto_inject_preview_chars``.  Notes
    below ``kb_auto_inject_min_score`` are filtered out.

    Returns an empty string when:
      - auto-injection is disabled in settings,
      - the user message is empty/whitespace,
      - the lookup returns no notes above the score cutoff,
      - any error occurs during the Weaviate query.
    """
    settings = get_settings()
    if not settings.kb_auto_inject_enabled:
        return ""

    query = (user_message or "").strip()
    if not query:
        return ""

    user_top_n = max(0, int(settings.kb_auto_inject_user_top_n))
    shared_top_n = max(0, int(settings.kb_auto_inject_shared_top_n))
    if user_top_n == 0 and shared_top_n == 0:
        return ""

    min_score = float(settings.kb_auto_inject_min_score)
    preview_chars = max(40, int(settings.kb_auto_inject_preview_chars))
    user_id_str = str(user_id)

    try:
        ctx = _make_lookup_context(org_id, user_id, dept_id)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("kb_context: could not build lookup context: %s", exc)
        return ""

    svc = KnowledgeService()
    try:
        # Single query covering both scopes (user + org-wide).  We pull a
        # small overshoot so the partition into user/shared can satisfy
        # both top-N caps without a second round-trip.
        limit = max(user_top_n + shared_top_n, 5)
        scored = await svc.search_similar_scored(
            query, ctx, user_scoped=True, limit=limit,
        )
    except Exception as exc:
        logger.warning("kb_context: lookup failed (%s) — skipping preamble", exc)
        return ""

    if not scored:
        return ""

    user_lines: list[str] = []
    shared_lines: list[str] = []
    for content, score, owner_id in scored:
        # Score may be None if Weaviate didn't return one; treat as
        # "unknown" and keep the entry only when the cutoff is 0.
        if score is not None and score < min_score:
            continue
        preview = _truncate(content, preview_chars)
        if not preview:
            continue
        if owner_id == user_id_str and len(user_lines) < user_top_n:
            user_lines.append(f"- (you) {preview}")
        elif owner_id != user_id_str and len(shared_lines) < shared_top_n:
            shared_lines.append(f"- (shared) {preview}")
        if len(user_lines) >= user_top_n and len(shared_lines) >= shared_top_n:
            break

    if not user_lines and not shared_lines:
        return ""

    body_lines: list[str] = [
        "Relevant saved notes (background context — do not echo verbatim, "
        "use search_knowledge_base for full content):",
    ]
    body_lines.extend(user_lines)
    body_lines.extend(shared_lines)
    return f"{_PREAMBLE_OPEN}\n" + "\n".join(body_lines) + f"\n{_PREAMBLE_CLOSE}"


async def prepend_kb_hints(
    user_message: str,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    dept_id: Optional[uuid.UUID] = None,
) -> str:
    """Convenience: return ``<kb_hints>…</kb_hints>\\n\\n{user_message}``.

    When the preamble would be empty, returns *user_message* unchanged.
    Use this at every ``agent.arun(...)`` call site.
    """
    preamble = await build_turn_preamble(
        user_message, org_id=org_id, user_id=user_id, dept_id=dept_id,
    )
    if not preamble:
        return user_message
    return f"{preamble}\n\n{user_message}"
