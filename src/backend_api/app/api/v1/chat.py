"""gSage AI — Chat Client API routes (Sprint 3).

All routes are scoped to a tenant via ``org_id`` path parameter and resolved
through :func:`get_tenant_context`.

Routes
------
- ``POST   /orgs/{org_id}/chat/conversations``            — create conversation
- ``GET    /orgs/{org_id}/chat/conversations``            — list conversations (paged)
- ``GET    /orgs/{org_id}/chat/conversations/{conv_id}``  — conversation detail
- ``PATCH  /orgs/{org_id}/chat/conversations/{conv_id}``  — rename / archive / move to folder
- ``DELETE /orgs/{org_id}/chat/conversations/{conv_id}``  — soft-delete
- ``POST   /orgs/{org_id}/chat/folders``                  — create folder
- ``GET    /orgs/{org_id}/chat/folders``                  — list folders
- ``PATCH  /orgs/{org_id}/chat/folders/{folder_id}``      — rename / archive (cascades)
- ``DELETE /orgs/{org_id}/chat/folders/{folder_id}``      — delete folder (ungroups conversations)
- ``GET    /orgs/{org_id}/chat/conversations/{conv_id}/messages``        — chat history
- ``POST   /orgs/{org_id}/chat/conversations/{conv_id}/messages``        — send message
- ``POST   /orgs/{org_id}/chat/conversations/{conv_id}/messages/stream`` — SSE stream
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, AsyncIterator, List, Optional, cast

from src.backend_api.app.schemas.pagination import PaginatedResponse, PaginationParams, paginate_query

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_tenant_context
from src.backend_api.app.core.tenant import TenantContext
from src.backend_api.app.schemas.chat import (
    ConversationCreate,
    ConversationOut,
    ConversationPatch,
    ContinueRunRequest,
    FolderCreate,
    FolderOut,
    FolderPatch,
    MessageMetadata,
    MessageOut,
    MessageTokenMetadata,
    SendMessageRequest,
    SendMessageResponse,
)
from src.backend_api.app.services.agent_factory import AGENT_REGISTRY, DEFAULT_AGENT_ID, _fetch_tool_catalog, build_agent, load_interface_profiles
import logging

from src.shared.database import get_db
from src.shared.services.response_filter import (
    FilterContext,
    StreamFilter,
    apply_filters_to_text,
)
from src.shared.models.approval_delegation import GSageApprovalDelegation
from src.shared.models.background_task import GSageBackgroundTask, BackgroundTaskStatus
from src.shared.models.conversation_folder import GSageConversationFolder
from src.shared.models.organization import GSageOrganization
from src.shared.models.tenant_session import GSageTenantSession
from src.shared.models.user import GSageUser

log = logging.getLogger(__name__)
router = APIRouter()

# Separate router for SSE stream — excluded from rate limiting (long-lived connection).
stream_router = APIRouter()

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _load_org(
    org_id: uuid.UUID,
    db: AsyncSession,
) -> Optional[GSageOrganization]:
    """Load the organization from DB (returns None on miss — factory uses .env defaults)."""
    result = await db.execute(
        select(GSageOrganization).where(GSageOrganization.id == org_id)
    )
    return result.scalar_one_or_none()


async def _load_user(
    user_id: uuid.UUID,
    db: AsyncSession,
) -> Optional[GSageUser]:
    """Load the user from DB (returns None on miss)."""
    result = await db.execute(
        select(GSageUser).where(GSageUser.id == user_id)
    )
    return result.scalar_one_or_none()


async def _get_conv_or_404(
    conv_id: uuid.UUID,
    ctx: TenantContext,
    db: AsyncSession,
) -> GSageTenantSession:
    """Lookup a conversation by id and validate tenant ownership.

    Raises HTTP 404 if not found or the session belongs to a different org.
    """
    result = await db.execute(
        select(GSageTenantSession).where(
            GSageTenantSession.id == conv_id,
            GSageTenantSession.org_id == ctx.org_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return session


async def _get_folder_or_404(
    folder_id: uuid.UUID,
    ctx: TenantContext,
    db: AsyncSession,
) -> GSageConversationFolder:
    """Lookup a folder by id and validate tenant + owner.

    Raises HTTP 404 if not found, or it belongs to a different org/user.
    """
    result = await db.execute(
        select(GSageConversationFolder).where(
            GSageConversationFolder.id == folder_id,
            GSageConversationFolder.org_id == ctx.org_id,
            GSageConversationFolder.user_id == ctx.user_id,
        )
    )
    folder = result.scalar_one_or_none()
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")
    return folder


def _extract_text(content) -> str:
    """Extract plain text from a RunOutput or Message content field.

    Handles: str, list of content blocks, or None.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif hasattr(block, "text"):
                parts.append(str(block.text))
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                else:
                    parts.append(str(block))
        return "".join(parts)
    return str(content)


def _build_tool_call_summary(tool_calls: list) -> str:
    """Build a human-readable summary for assistant messages that only contain tool calls."""
    summaries: list[str] = []
    for tc in tool_calls:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = fn.get("name", "") if isinstance(fn, dict) else ""
        args_raw = fn.get("arguments", "") if isinstance(fn, dict) else ""
        if not name:
            continue
        label = name.replace("_", " ").replace("-", " ")
        # Parse arguments to build a compact param summary
        params_str = ""
        if args_raw:
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                if isinstance(args, dict):
                    # Filter out internal params starting with _
                    visible = {
                        k: v for k, v in args.items()
                        if not k.startswith("_") and v is not None
                    }
                    if visible:
                        parts = []
                        for k, v in visible.items():
                            val = str(v)
                            if len(val) > 80:
                                val = val[:77] + "..."
                            parts.append(f"**{k}**: {val}")
                        params_str = " — " + ", ".join(parts)
            except (json.JSONDecodeError, TypeError):
                pass
        summaries.append(f"🔧 *{label}*{params_str}")
    if not summaries:
        return "⏳ Processing…"
    return "\n".join(summaries)


def _fmt_sse(event: str, data: dict) -> str:
    """Format a single SSE frame."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _get_pending_bg_notifications(
    gsage_session_id: uuid.UUID,
    db: AsyncSession,
) -> list[GSageBackgroundTask]:
    from src.backend_api.app.services.background_tasks import get_pending_bg_notifications
    return await get_pending_bg_notifications(gsage_session_id, db)


def _build_bg_context_block(tasks: list[GSageBackgroundTask]) -> str:
    from src.backend_api.app.services.background_tasks import build_bg_context_block
    return build_bg_context_block(tasks)


async def _mark_bg_tasks_notified(
    task_ids: list[uuid.UUID],
    db: AsyncSession,
) -> None:
    from src.backend_api.app.services.background_tasks import mark_bg_tasks_notified
    await mark_bg_tasks_notified(task_ids, db)
    # Commit immediately: this helper is always called outside a session.begin()
    # context (FastAPI dependency-injected session uses autobegin).
    try:
        await db.commit()
    except Exception as exc:
        log.warning("_mark_bg_tasks_notified: commit failed: %s", exc)
        await db.rollback()


async def _resolve_attachments(
    attachment_ids: list[uuid.UUID],
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    conv_id: uuid.UUID,
    db: AsyncSession,
) -> list[dict]:
    """Resolve a list of attachment IDs to file metadata dicts.

    Silently drops IDs that cannot be found or accessed.  Each dict
    contains ``file_id``, ``filename``, ``content_type``, ``size_bytes``.
    """
    if not attachment_ids:
        return []
    from src.shared.models.generated_file import GSageFile

    result = await db.execute(
        select(GSageFile).where(
            GSageFile.id.in_(attachment_ids),
            GSageFile.org_id == org_id,
            GSageFile.category == "attachment",
            GSageFile.purged_at.is_(None),
        )
    )
    rows = result.scalars().all()
    accessible = []
    for row in rows:
        # User must own the file or it must be org-scoped
        if str(row.user_id) != str(user_id) and row.scope != "organization":
            continue
        file_id_str = str(row.id)
        accessible.append({
            "file_id": file_id_str,
            "filename": row.filename,
            "content_type": row.content_type,
            "size_bytes": row.size_bytes,
            "download_path": f"/v1/orgs/{org_id}/files/{file_id_str}/download",
        })
    return accessible


async def _load_dept_name(dept_id: uuid.UUID, db: AsyncSession) -> Optional[str]:
    """Return the department name for a given dept_id, or None on any error."""
    try:
        from src.shared.models.department import GSageDepartment  # noqa: PLC0415
        result = await db.execute(
            select(GSageDepartment).where(GSageDepartment.id == dept_id)
        )
        dept = result.scalar_one_or_none()
        return dept.name if dept else None
    except Exception:
        return None


def _build_dept_context_block(dept_id: uuid.UUID, dept_name: Optional[str]) -> str:
    """Build the [DEPARTMENT_CONTEXT] injection block for the agent message."""
    name_str = dept_name or str(dept_id)
    return (
        "[DEPARTMENT_CONTEXT]\n"
        f"The user's active department is: {name_str} (ID: {dept_id})\n"
        "All department-scoped operations (datastores, files, tool configs) "
        "must use this department context automatically. "
        "Do NOT ask the user to define a department.\n"
        "[/DEPARTMENT_CONTEXT]"
    )


def _build_attachment_block(attachments: list[dict]) -> str:
    """Build the [ATTACHED_FILES] injection block for the LLM context."""
    lines = ["[ATTACHED_FILES]"]
    for att in attachments:
        size_kb = att["size_bytes"] / 1024
        download_path = att.get("download_path", "")
        lines.append(
            f"- {att['filename']} (id: {att['file_id']}, type: {att['content_type']},"
            f" size: {size_kb:.1f} KB, download: {download_path})"
        )
    lines.append("[/ATTACHED_FILES]")
    return "\n".join(lines)


async def _process_approval_delegations(
    *,
    approval_ids: list[str],
    ctx: TenantContext,
    db: AsyncSession,
    org: Optional[GSageOrganization],
    agno_session_id: str,
    run_id: str,
) -> None:
    """Delegate to shared approval_delegations service.

    Always called outside a session.begin() context here (FastAPI dep-injected
    session uses autobegin), so we commit immediately after.
    """
    from src.backend_api.app.services.approval_delegations import process_approval_delegations
    await process_approval_delegations(
        approval_ids=approval_ids,
        ctx=ctx,
        db=db,
        org=org,
        agno_session_id=agno_session_id,
        run_id=run_id,
    )
    try:
        await db.commit()
    except Exception as exc:
        log.warning("_process_approval_delegations: commit failed: %s", exc)
        await db.rollback()


async def _process_auto_approvals(
    *,
    approval_ids: list[str],
    ctx: TenantContext,
    db: AsyncSession,
) -> tuple[list[str], list[str]]:
    """Partition pending approvals into auto-approved vs manual.

    For each id flagged as auto-approve (DB toolconfig > env > default False),
    immediately resolves the Agno approval row as ``approved`` and dispatches
    the continuation Celery task — the same path used when a human clicks
    "Approve" in the UI.

    Returns ``(auto_ids, manual_ids)``. Auto-resolved ids should be excluded
    from delegation processing and from the ``run_paused`` payload emitted
    to the client.

    Errors per approval are logged and the id is treated as manual to fail
    safe (a human is still asked).
    """
    if not approval_ids:
        return [], []

    from src.backend_api.app.services.agent_factory import get_agno_db
    from src.backend_api.app.services.tool_auto_approve import is_auto_approve

    agno_db = get_agno_db()
    auto_ids: list[str] = []
    manual_ids: list[str] = []

    for ap_id in approval_ids:
        try:
            ap_row = await agno_db.get_approval(ap_id)
            if ap_row is None:
                log.warning("auto_approve: approval %s not found in Agno DB", ap_id)
                manual_ids.append(ap_id)
                continue

            tool_name: str = ap_row.get("tool_name") or "*"
            tool_args: dict = dict(ap_row.get("tool_args") or {})
            # Unwrap proxy tool names (run_discovered_tool / run_approved_tool)
            if tool_name in ("run_discovered_tool", "run_approved_tool") and "tool_name" in tool_args:
                tool_name = tool_args["tool_name"] or tool_name

            enabled = await is_auto_approve(
                org_id=ctx.org_id, tool_name=tool_name,
            )
            if not enabled:
                manual_ids.append(ap_id)
                continue

            updated = await agno_db.update_approval(
                ap_id,
                expected_status="pending",
                status="approved",
                resolved_by=str(ctx.user_id),
                resolved_at=int(datetime.now(timezone.utc).timestamp()),
                resolution_data={
                    "action": "approve",
                    "auto_approved": True,
                    "comment": "Auto-approved by tool config",
                },
            )
            if updated is None:
                log.warning(
                    "auto_approve: update_approval returned None for ap=%s tool=%s",
                    ap_id, tool_name,
                )
                manual_ids.append(ap_id)
                continue

            try:
                from src.backend_api.app.tasks.agent_continuation import (  # noqa: PLC0415
                    continue_after_approval_resolved,
                )
                cast(Any, continue_after_approval_resolved).delay(
                    ap_id, str(ctx.org_id)
                )
            except Exception as cont_exc:
                log.error(
                    "auto_approve: failed to dispatch continuation ap=%s: %s",
                    ap_id, cont_exc, exc_info=True,
                )
                # Continuation failed but the approval row is already
                # resolved — leaving it in manual_ids would mislead the UI.
                # Keep it in auto_ids and surface via logs.
            auto_ids.append(ap_id)
            log.info(
                "auto-approval: tool=%s approval=%s org=%s user=%s",
                tool_name, ap_id, ctx.org_id, ctx.user_id,
            )
        except Exception as exc:
            log.error(
                "auto_approve: unexpected error for ap=%s: %s",
                ap_id, exc, exc_info=True,
            )
            manual_ids.append(ap_id)

    return auto_ids, manual_ids


# ---------------------------------------------------------------------------
# LLM retry helpers
# ---------------------------------------------------------------------------

_LLM_RETRY_ATTEMPTS = 2           # number of retries (total up to 3 attempts)
_LLM_RETRY_BASE_DELAY_SECONDS = 2.0  # base delay; doubles each retry (2s, 4s …)
_LLM_TOTAL_ATTEMPTS = _LLM_RETRY_ATTEMPTS + 1

_LLM_UNAVAILABLE_MSG = (
    f"We tried to reach the LLM service {_LLM_TOTAL_ATTEMPTS} times "
    "but were unable to get a response. Please try again later "
    "or contact your administrator."
)

# Surfaced when another run (e.g. a background-tool continuation) is
# currently active on the same Agno session and we cannot acquire the
# per-session lock in time.  The user is asked to retry shortly.
_LLM_SESSION_BUSY_MSG = (
    "Another response is still being generated for this conversation. "
    "Please wait a moment and try again."
)


def _is_transient_llm_error(text: str) -> bool:
    """Return True for transient provider errors that are safe to retry."""
    t = text.lower()
    return "503" in t or "service unavailable" in t or "unavailable" in t


_MCP_CLEANUP_TIMEOUT = 5.0  # seconds to wait for graceful MCP session cleanup


async def _cleanup_agent_mcp(agent, *, timeout: float = _MCP_CLEANUP_TIMEOUT) -> None:
    """Thin wrapper that delegates to the shared implementation.

    Kept here (and re-exported) because other modules import
    ``_cleanup_agent_mcp`` from this file by name.
    """
    from src.shared.services.mcp_cleanup import cleanup_agent_mcp

    await cleanup_agent_mcp(agent, timeout=timeout)


async def _run_with_retry(coro_fn, *, context: str = "agent") -> Any:
    """Execute ``await coro_fn()`` with automatic retry on transient LLM errors.

    Handles both raised exceptions and agno's ``RunStatus.error`` return value
    (agno swallows some provider HTTP errors and returns an error-status RunOutput).

    Raises :exc:`HTTPException` 502 with a user-friendly message after all
    retries are exhausted or on non-transient errors.
    """
    from agno.run import RunStatus

    retries_left = _LLM_RETRY_ATTEMPTS
    attempt = 0
    while True:
        attempt += 1
        try:
            result = await coro_fn()
            # Agno may swallow HTTP errors (e.g. 503) and return error status
            if getattr(result, "status", None) == RunStatus.error:
                err_str = str(getattr(result, "content", ""))
                if retries_left > 0 and _is_transient_llm_error(err_str):
                    retries_left -= 1
                    delay = _LLM_RETRY_BASE_DELAY_SECONDS * (2 ** (_LLM_RETRY_ATTEMPTS - retries_left - 1))
                    log.warning(
                        "LLM error status [%s], retrying in %.1fs (%d left): %s",
                        context, delay, retries_left, err_str,
                    )
                    await asyncio.sleep(delay)
                    continue
                log.error("Agent run returned error [%s]: %s", context, err_str)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=_LLM_UNAVAILABLE_MSG,
                )
            return result
        except HTTPException:
            raise
        except Exception as exc:
            if retries_left > 0 and _is_transient_llm_error(str(exc)):
                retries_left -= 1
                delay = _LLM_RETRY_BASE_DELAY_SECONDS * (2 ** (_LLM_RETRY_ATTEMPTS - retries_left - 1))
                log.warning(
                    "Transient LLM exception [%s], retrying in %.1fs (%d left): %s",
                    context, delay, retries_left, exc,
                )
                await asyncio.sleep(delay)
                continue
            log.error("Agent run failed [%s]: %s", context, exc, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=_LLM_UNAVAILABLE_MSG,
            ) from exc


# ---------------------------------------------------------------------------
# 1. Create conversation
# ---------------------------------------------------------------------------


@router.post(
    "/orgs/{org_id}/chat/conversations",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new conversation",
)
async def create_conversation(
    org_id: uuid.UUID,
    payload: ConversationCreate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationOut:
    """Create a new conversation (GSageTenantSession).

    The Agno session is created lazily when the first message is sent.
    The ``agno_session_id`` is pre-generated here so clients can reference it.
    """
    ctx.require_permission("agents:run")

    agent_id = payload.agent_id or "assistant"
    if agent_id not in AGENT_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown agent_id '{agent_id}'. Available: {list(AGENT_REGISTRY)}",
        )

    conv_uuid = uuid.uuid4()
    agno_session_id = ctx.build_session_id("conv", str(conv_uuid))

    # Validate the target folder (if any) belongs to the current org + user.
    if payload.folder_id is not None:
        await _get_folder_or_404(payload.folder_id, ctx, db)

    tenant_session = GSageTenantSession(
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        agno_session_id=agno_session_id,
        title=payload.title,
        folder_id=payload.folder_id,
    )
    db.add(tenant_session)
    await db.commit()
    await db.refresh(tenant_session)

    return ConversationOut(
        id=tenant_session.id,
        agno_session_id=tenant_session.agno_session_id,
        title=tenant_session.title,
        is_active=tenant_session.is_active,
        folder_id=tenant_session.folder_id,
        agent_id=agent_id,
        created_at=tenant_session.created_at,
        updated_at=tenant_session.updated_at,
    )


# ---------------------------------------------------------------------------
# 2. List conversations
# ---------------------------------------------------------------------------


@router.get(
    "/orgs/{org_id}/chat/conversations",
    response_model=PaginatedResponse[ConversationOut],
    summary="List conversations for the current user",
)
async def list_conversations(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends()],
    active_only: bool = Query(default=True, alias="active"),
) -> PaginatedResponse[ConversationOut]:
    """List conversations belonging to the current user in this org."""
    ctx.require_permission("sessions:read")

    stmt = select(GSageTenantSession).where(
        GSageTenantSession.org_id == ctx.org_id,
        GSageTenantSession.user_id == ctx.user_id,
    )
    if active_only:
        stmt = stmt.where(GSageTenantSession.is_active == True)  # noqa: E712
    stmt = stmt.order_by(GSageTenantSession.updated_at.desc())

    sessions, total = await paginate_query(db, stmt, pagination)

    items = [
        ConversationOut(
            id=s.id,
            agno_session_id=s.agno_session_id,
            title=s.title,
            is_active=s.is_active,
            folder_id=s.folder_id,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in sessions
    ]
    return PaginatedResponse.build(items, total=total, pagination=pagination)


# ---------------------------------------------------------------------------
# 3. Get conversation detail
# ---------------------------------------------------------------------------


@router.get(
    "/orgs/{org_id}/chat/conversations/{conv_id}",
    response_model=ConversationOut,
    summary="Get conversation detail",
)
async def get_conversation(
    org_id: uuid.UUID,
    conv_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationOut:
    ctx.require_permission("sessions:read")
    session = await _get_conv_or_404(conv_id, ctx, db)
    return ConversationOut(
        id=session.id,
        agno_session_id=session.agno_session_id,
        title=session.title,
        is_active=session.is_active,
        folder_id=session.folder_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


# ---------------------------------------------------------------------------
# 4. Patch conversation
# ---------------------------------------------------------------------------


@router.patch(
    "/orgs/{org_id}/chat/conversations/{conv_id}",
    response_model=ConversationOut,
    summary="Rename or archive a conversation",
)
async def patch_conversation(
    org_id: uuid.UUID,
    conv_id: uuid.UUID,
    payload: ConversationPatch,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationOut:
    ctx.require_permission("sessions:read")
    session = await _get_conv_or_404(conv_id, ctx, db)

    if payload.title is not None:
        session.title = payload.title
    if payload.is_active is not None:
        session.is_active = payload.is_active
    if payload.clear_folder:
        session.folder_id = None
    elif payload.folder_id is not None:
        # Validate the target folder belongs to the same org + user.
        await _get_folder_or_404(payload.folder_id, ctx, db)
        session.folder_id = payload.folder_id

    await db.commit()
    await db.refresh(session)

    return ConversationOut(
        id=session.id,
        agno_session_id=session.agno_session_id,
        title=session.title,
        is_active=session.is_active,
        folder_id=session.folder_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


# ---------------------------------------------------------------------------
# 5. Delete conversation (soft)
# ---------------------------------------------------------------------------


@router.delete(
    "/orgs/{org_id}/chat/conversations/{conv_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a conversation",
)
async def delete_conversation(
    org_id: uuid.UUID,
    conv_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """`is_active = False`. The Agno session is kept for audit purposes."""
    ctx.require_permission("sessions:delete")
    session = await _get_conv_or_404(conv_id, ctx, db)
    session.is_active = False
    await db.commit()


# ---------------------------------------------------------------------------
# 5b. Conversation folders
# ---------------------------------------------------------------------------


async def _folder_to_out(
    folder: GSageConversationFolder,
    db: AsyncSession,
) -> FolderOut:
    """Serialize a folder, counting its active conversations."""
    count_result = await db.execute(
        select(func.count())
        .select_from(GSageTenantSession)
        .where(
            GSageTenantSession.folder_id == folder.id,
            GSageTenantSession.is_active == True,  # noqa: E712
        )
    )
    conversation_count = int(count_result.scalar_one() or 0)
    return FolderOut(
        id=folder.id,
        name=folder.name,
        is_active=folder.is_active,
        conversation_count=conversation_count,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


@router.post(
    "/orgs/{org_id}/chat/folders",
    response_model=FolderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a conversation folder",
)
async def create_folder(
    org_id: uuid.UUID,
    payload: FolderCreate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FolderOut:
    """Create a folder owned by the current user in this org."""
    ctx.require_permission("sessions:read")
    folder = GSageConversationFolder(
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        name=payload.name,
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return await _folder_to_out(folder, db)


@router.get(
    "/orgs/{org_id}/chat/folders",
    response_model=List[FolderOut],
    summary="List the current user's conversation folders",
)
async def list_folders(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    active_only: bool = Query(default=True, alias="active"),
) -> List[FolderOut]:
    """List folders owned by the current user, ordered by name."""
    ctx.require_permission("sessions:read")
    stmt = select(GSageConversationFolder).where(
        GSageConversationFolder.org_id == ctx.org_id,
        GSageConversationFolder.user_id == ctx.user_id,
    )
    if active_only:
        stmt = stmt.where(GSageConversationFolder.is_active == True)  # noqa: E712
    stmt = stmt.order_by(GSageConversationFolder.name.asc())

    result = await db.execute(stmt)
    folders = result.scalars().all()
    return [await _folder_to_out(f, db) for f in folders]


@router.patch(
    "/orgs/{org_id}/chat/folders/{folder_id}",
    response_model=FolderOut,
    summary="Rename or archive a folder (cascades to its conversations)",
)
async def patch_folder(
    org_id: uuid.UUID,
    folder_id: uuid.UUID,
    payload: FolderPatch,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FolderOut:
    """Rename and/or archive a folder.

    Archiving (``is_active=false``) cascades: every conversation in the folder
    is archived too. Un-archiving cascades symmetrically, restoring them.
    """
    ctx.require_permission("sessions:read")
    folder = await _get_folder_or_404(folder_id, ctx, db)

    if payload.name is not None:
        folder.name = payload.name
    if payload.is_active is not None and payload.is_active != folder.is_active:
        folder.is_active = payload.is_active
        # Symmetric cascade to the folder's conversations.
        await db.execute(
            GSageTenantSession.__table__.update()
            .where(
                GSageTenantSession.folder_id == folder.id,
                GSageTenantSession.org_id == ctx.org_id,
            )
            .values(is_active=payload.is_active)
        )

    await db.commit()
    await db.refresh(folder)
    return await _folder_to_out(folder, db)


@router.delete(
    "/orgs/{org_id}/chat/folders/{folder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a folder (conversations are moved to ungrouped)",
)
async def delete_folder(
    org_id: uuid.UUID,
    folder_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Hard-delete a folder. Its conversations keep existing but become ungrouped.

    The ``folder_id`` FK uses ``ON DELETE SET NULL``, so conversations are
    detached automatically rather than deleted.
    """
    ctx.require_permission("sessions:delete")
    folder = await _get_folder_or_404(folder_id, ctx, db)
    await db.delete(folder)
    await db.commit()


# ---------------------------------------------------------------------------
# 6. List messages (read from Agno DB)
# ---------------------------------------------------------------------------


@router.get(
    "/orgs/{org_id}/chat/conversations/{conv_id}/messages",
    response_model=List[MessageOut],
    summary="List messages in a conversation",
)
async def list_messages(
    org_id: uuid.UUID,
    conv_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
    last_n: Optional[int] = Query(default=None, ge=1, le=200),
) -> List[MessageOut]:
    """Read conversation history directly from the Agno session table.

    Uses ``AgentSession.get_chat_history()`` (user + assistant messages only).
    """
    ctx.require_permission("sessions:read")
    session = await _get_conv_or_404(conv_id, ctx, db)

    from agno.db.base import SessionType
    from agno.run import RunStatus
    from src.backend_api.app.services.agent_factory import get_agno_db

    agno_session = await get_agno_db().get_session(
        session_id=session.agno_session_id,
        session_type=SessionType.AGENT,
    )
    if agno_session is None:
        return []

    # Iterate runs directly so we can attach the run-level status to each
    # projected MessageOut. This is essential for surfacing failed runs to
    # the frontend (which renders an error badge): if we use the default
    # ``get_messages`` it silently filters runs whose status is ``error``
    # or ``cancelled``, causing the user-visible chat to "lose" turns.
    out: list[MessageOut] = []
    runs = list(getattr(agno_session, "runs", None) or [])
    # Skip runs that are part of team members and apply ``last_n`` here.
    runs = [r for r in runs if getattr(r, "parent_run_id", None) is None]
    if last_n is not None and last_n > 0:
        runs = runs[-last_n:]

    for run in runs:
        run_status = getattr(run, "status", None)
        # Skip cancelled runs entirely (user-initiated cancellation has no
        # value in the chat history).
        if run_status == RunStatus.cancelled:
            continue

        # Map the run status to a user-visible message status badge.
        # ``error`` and ``paused`` are surfaced; everything else is None.
        status_str: Optional[str] = None
        if run_status == RunStatus.error:
            status_str = "error"
        elif run_status == RunStatus.paused:
            status_str = "paused"

        run_messages = list(getattr(run, "messages", None) or [])

        # When the run failed and produced no assistant message (or only an
        # empty one), synthesize a friendly assistant message so the user
        # understands the turn ended in error rather than seeing nothing.
        has_visible_assistant = any(
            getattr(m, "role", None) == "assistant"
            and (
                _extract_text(getattr(m, "content", None)).strip()
                or getattr(m, "tool_calls", None)
            )
            for m in run_messages
        )

        for msg in run_messages:
            role = getattr(msg, "role", "assistant")
            if role in ("system", "tool"):
                continue
            # Skip history messages tagged from previous runs.
            if getattr(msg, "from_history", False):
                continue

            content_str = _extract_text(getattr(msg, "content", None))
            created_at: Optional[datetime] = getattr(msg, "created_at", None)

            # Strip internal injection blocks from user messages.  These blocks
            # are prepended by the backend before sending to the LLM and should
            # never be shown to the end-user.
            if role == "user" and (
                "[BACKGROUND_TASKS_COMPLETED]" in content_str
                or "[ATTACHED_FILES]" in content_str
                or "[DEPARTMENT_CONTEXT]" in content_str
            ):
                import re as _re
                content_str = _re.sub(
                    r"\[BACKGROUND_TASKS_COMPLETED\].*?\[/BACKGROUND_TASKS_COMPLETED\]\s*---\s*",
                    "",
                    content_str,
                    flags=_re.DOTALL,
                )
                content_str = _re.sub(
                    r"\[ATTACHED_FILES\].*?\[/ATTACHED_FILES\]\s*---\s*",
                    "",
                    content_str,
                    flags=_re.DOTALL,
                )
                content_str = _re.sub(
                    r"\[DEPARTMENT_CONTEXT\].*?\[/DEPARTMENT_CONTEXT\]\s*---\s*",
                    "",
                    content_str,
                    flags=_re.DOTALL,
                ).strip()
                if not content_str:
                    continue

            # Assistant messages with no textual content are tool-call-only messages.
            # Enrich them with a summary of the tool(s) being invoked.
            if role == "assistant" and not content_str.strip():
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    content_str = _build_tool_call_summary(tool_calls)
                else:
                    # Completely empty message — skip it
                    continue

            out.append(
                MessageOut(
                    id=getattr(msg, "id", None),
                    role=role,
                    content=content_str,
                    created_at=created_at,
                    status=status_str if role == "assistant" else None,
                )
            )

        # Synthesize a placeholder assistant message for failed runs that
        # produced no visible assistant output, so the user sees a clear
        # failure indicator instead of an empty turn.
        if status_str == "error" and not has_visible_assistant:
            err_text = _extract_text(getattr(run, "content", None)).strip()
            if not err_text:
                err_text = (
                    "(The agent could not complete this response. "
                    "Please try again.)"
                )
            else:
                err_text = f"(The agent could not complete this response: {err_text})"
            out.append(
                MessageOut(
                    id=getattr(run, "run_id", None),
                    role="assistant",
                    content=err_text,
                    created_at=getattr(run, "created_at", None),
                    status="error",
                )
            )

    # -- Polling hints --------------------------------------------------------
    # Tell the frontend whether it should keep polling for new messages.
    # This is essential when the user navigates away and back — the SSE-driven
    # flags (pendingApprovals, hasActiveBgTasks) are lost on navigation.
    try:
        needs_polling = False
        has_pending_approvals = False

        from src.backend_api.app.services.background_tasks import (
            has_active_bg_tasks,
            get_pending_bg_notifications,
        )

        if await has_active_bg_tasks(session.id, db):
            needs_polling = True
        elif await get_pending_bg_notifications(session.id, db):
            needs_polling = True

        # Pending approval delegations (not yet continued)
        from src.shared.models.approval_delegation import GSageApprovalDelegation

        deleg_pending = (await db.execute(
            select(GSageApprovalDelegation.id).where(
                GSageApprovalDelegation.agno_session_id == session.agno_session_id,
                GSageApprovalDelegation.continued_at.is_(None),
            ).limit(1)
        )).first()
        if deleg_pending is not None:
            needs_polling = True
            has_pending_approvals = True

        # Recent continuations (within last 2 min) — agent may still be
        # generating a response after acontinue_run() was dispatched.
        if not needs_polling:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=120)
            recent_cont = (await db.execute(
                select(GSageApprovalDelegation.id).where(
                    GSageApprovalDelegation.agno_session_id == session.agno_session_id,
                    GSageApprovalDelegation.continued_at.isnot(None),
                    GSageApprovalDelegation.continued_at >= cutoff,
                ).limit(1)
            )).first()
            if recent_cont is not None:
                needs_polling = True

        if needs_polling:
            response.headers["X-Needs-Polling"] = "true"
        if has_pending_approvals:
            response.headers["X-Has-Pending-Approvals"] = "true"
    except Exception:
        pass  # best-effort; polling hints are not critical

    return out


# ---------------------------------------------------------------------------
# 7. Send message (synchronous)
# ---------------------------------------------------------------------------


@router.post(
    "/orgs/{org_id}/chat/conversations/{conv_id}/messages",
    response_model=SendMessageResponse,
    summary="Send a message and get a synchronous reply",
)
async def send_message(
    org_id: uuid.UUID,
    conv_id: uuid.UUID,
    payload: SendMessageRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SendMessageResponse:
    """Invoke the agent and return the full response as JSON."""
    ctx.require_permission("agents:run")
    session = await _get_conv_or_404(conv_id, ctx, db)

    org = await _load_org(ctx.org_id, db)
    user = await _load_user(ctx.user_id, db)
    profile_org, profile_user = await load_interface_profiles(
        ctx.org_id, ctx.user_id, ctx.interface, db
    )
    tool_catalog = await _fetch_tool_catalog(ctx, gsage_session_id=session.id)
    agent = build_agent(
        ctx=ctx,
        agent_id=DEFAULT_AGENT_ID,
        session_id=session.agno_session_id,
        org=org,
        user=user,
        interface_profile_org=profile_org,
        interface_profile_user=profile_user,
        gsage_session_id=session.id,
        tool_catalog=tool_catalog,
    )

    # Inject any completed background task results that have not been notified
    # yet for this conversation, so the LLM can summarise them to the user.
    pending_bg_tasks = await _get_pending_bg_notifications(session.id, db)
    effective_message = payload.message
    if pending_bg_tasks:
        bg_block = _build_bg_context_block(pending_bg_tasks)
        effective_message = f"{bg_block}\n\n---\n{payload.message}"

    # Inject attachment metadata so the LLM knows what files are attached.
    attachments = await _resolve_attachments(
        payload.attachment_ids,
        org_id=org_id,
        user_id=ctx.user_id,
        conv_id=conv_id,
        db=db,
    )
    if attachments:
        att_block = _build_attachment_block(attachments)
        effective_message = f"{att_block}\n\n---\n{effective_message}"

    # Inject active department context so the agent knows which dept is selected
    # and can use it directly in tool calls without asking the user.
    if ctx.dept_id is not None:
        dept_name = await _load_dept_name(ctx.dept_id, db)
        dept_block = _build_dept_context_block(ctx.dept_id, dept_name)
        effective_message = f"{dept_block}\n\n---\n{effective_message}"

    # Auto-inject KB hints (saved notes/memories) so the LLM is reminded
    # of relevant memories without having to call ``search_knowledge_base``.
    # Failure is absorbed inside the helper.
    from src.shared.services.kb_context import prepend_kb_hints

    effective_message = await prepend_kb_hints(
        effective_message,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        dept_id=ctx.dept_id,
    )

    try:
        from src.backend_api.app.services.agno_session_lock import (  # noqa: PLC0415
            LockAcquireError,
            acquire as _acquire_session_lock,
            publish_conversation_updated,
        )
        try:
            async with _acquire_session_lock(
                session.agno_session_id, owner="sync:send_message"
            ):
                run_output = await _run_with_retry(
                    lambda: agent.arun(effective_message),
                    context=f"send_message conv={conv_id}",
                )
        except LockAcquireError as exc:
            log.warning(
                "send_message: session %s busy: %s",
                session.agno_session_id, exc,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_LLM_SESSION_BUSY_MSG,
            )
    finally:
        await _cleanup_agent_mcp(agent)

    from agno.run import RunStatus

    # Mark injected tasks as notified (after the agent run completes)
    if pending_bg_tasks:
        await _mark_bg_tasks_notified([t.id for t in pending_bg_tasks], db)

    # ── HITL: run paused waiting for human approval ───────────────────────
    if run_output.status == RunStatus.paused:
        pending_approval_ids: list[str] = []
        for req in run_output.requirements or []:
            te = getattr(req, "tool_execution", None)
            approval_id = getattr(te, "approval_id", None) if te else None
            if approval_id:
                pending_approval_ids.append(str(approval_id))

        log.info(
            "Agent run paused conv=%s run_id=%s approvals=%s",
            conv_id, run_output.run_id, pending_approval_ids,
        )

        # ── Auto-approval: resolve flagged tools and only delegate the rest ──
        try:
            auto_ids, manual_ids = await _process_auto_approvals(
                approval_ids=pending_approval_ids,
                ctx=ctx,
                db=db,
            )
        except Exception as auto_exc:
            log.error(
                "sync auto-approval processing error: %s",
                auto_exc, exc_info=True,
            )
            auto_ids, manual_ids = [], list(pending_approval_ids)
        pending_approval_ids = manual_ids

        # ── Auto-delegation: create GSageApprovalDelegation rows ──────
        await _process_approval_delegations(
            approval_ids=pending_approval_ids,
            ctx=ctx,
            db=db,
            org=org,
            agno_session_id=session.agno_session_id,
            run_id=str(run_output.run_id or ""),
        )

        # When every approval was auto-resolved, treat as a regular response:
        # the continuation Celery task will deliver the assistant's next
        # message; surface that to the client via status.
        response_status = "pending_approval" if pending_approval_ids else "auto_approved"
        default_content = (
            "This action requires human approval before it can be executed. "
            "Please review the pending approvals and use POST .../messages/continue "
            "once they have been resolved."
            if pending_approval_ids
            else "The requested action was auto-approved by policy and is being executed."
        )

        return SendMessageResponse(
            id=run_output.run_id or str(uuid.uuid4()),
            session_id=str(session.id),
            agno_session_id=session.agno_session_id,
            role="assistant",
            content=_extract_text(run_output.content) or default_content,
            created_at=datetime.now(timezone.utc),
            metadata=MessageMetadata(run_id=run_output.run_id),
            status=response_status,
            pending_run_id=run_output.run_id if pending_approval_ids else None,
            pending_approvals=pending_approval_ids or None,
        )

    # ── Normal completed run ──────────────────────────────────────────────
    content = await apply_filters_to_text(
        _extract_text(run_output.content),
        FilterContext(org_id=ctx.org_id, interface=ctx.interface, db=db),
    )
    metrics = getattr(run_output, "metrics", None)

    return SendMessageResponse(
        id=run_output.run_id or str(uuid.uuid4()),
        session_id=str(session.id),
        agno_session_id=session.agno_session_id,
        role="assistant",
        content=content,
        created_at=datetime.now(timezone.utc),
        metadata=MessageMetadata(
            run_id=run_output.run_id,
            tokens=MessageTokenMetadata(
                input=getattr(metrics, "input_tokens", None) if metrics else None,
                output=getattr(metrics, "output_tokens", None) if metrics else None,
            ) if metrics else None,
            duration_ms=(
                int(getattr(metrics, "duration", 0) * 1000)
                if metrics and getattr(metrics, "duration", None)
                else None
            ),
        ),
    )


# ---------------------------------------------------------------------------
# 8. Continue a paused run (HITL approval resolved)
# ---------------------------------------------------------------------------


@router.post(
    "/orgs/{org_id}/chat/conversations/{conv_id}/messages/continue",
    response_model=SendMessageResponse,
    summary="Resume a paused run after approval(s) have been resolved",
)
async def continue_run(
    org_id: uuid.UUID,
    conv_id: uuid.UUID,
    payload: ContinueRunRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SendMessageResponse:
    """Resume an agent run that was paused because a tool required approval.

    After calling ``POST /orgs/{org_id}/approvals/{id}/resolve`` for every
    pending approval, pass the original ``run_id`` here and the agent will
    reload the saved run state, check the approval decisions stored in the
    Agno DB, and continue execution.
    """
    ctx.require_permission("agents:run")
    session = await _get_conv_or_404(conv_id, ctx, db)

    org = await _load_org(ctx.org_id, db)
    user = await _load_user(ctx.user_id, db)
    profile_org, profile_user = await load_interface_profiles(
        ctx.org_id, ctx.user_id, ctx.interface, db
    )
    tool_catalog = await _fetch_tool_catalog(ctx, gsage_session_id=session.id)
    agent = build_agent(
        ctx=ctx,
        agent_id=DEFAULT_AGENT_ID,
        session_id=session.agno_session_id,
        org=org,
        user=user,
        interface_profile_org=profile_org,
        interface_profile_user=profile_user,
        gsage_session_id=session.id,
        tool_catalog=tool_catalog,
    )

    try:
        run_output = await _run_with_retry(
            lambda: agent.acontinue_run(run_id=payload.run_id),
            context=f"continue_run conv={conv_id} run={payload.run_id}",
        )
    finally:
        await _cleanup_agent_mcp(agent)

    from agno.run import RunStatus

    # Still paused — more approvals required (multi-step HITL)
    if run_output.status == RunStatus.paused:
        pending_approval_ids: list[str] = []
        for req in run_output.requirements or []:
            te = getattr(req, "tool_execution", None)
            approval_id = getattr(te, "approval_id", None) if te else None
            if approval_id:
                pending_approval_ids.append(str(approval_id))

        # Auto-approval: resolve flagged tools and only return the manual rest.
        try:
            auto_ids, manual_ids = await _process_auto_approvals(
                approval_ids=pending_approval_ids,
                ctx=ctx,
                db=db,
            )
        except Exception as auto_exc:
            log.error(
                "continue_run auto-approval processing error: %s",
                auto_exc, exc_info=True,
            )
            auto_ids, manual_ids = [], list(pending_approval_ids)
        pending_approval_ids = manual_ids

        response_status = "pending_approval" if pending_approval_ids else "auto_approved"
        default_content = (
            "Additional approvals are required. Please resolve all pending approvals "
            "and call this endpoint again."
            if pending_approval_ids
            else "Pending approvals were auto-approved by policy and execution is continuing."
        )

        return SendMessageResponse(
            id=run_output.run_id or str(uuid.uuid4()),
            session_id=str(session.id),
            agno_session_id=session.agno_session_id,
            role="assistant",
            content=_extract_text(run_output.content) or default_content,
            created_at=datetime.now(timezone.utc),
            metadata=MessageMetadata(run_id=run_output.run_id),
            status=response_status,
            pending_run_id=run_output.run_id if pending_approval_ids else None,
            pending_approvals=pending_approval_ids or None,
        )

    content = await apply_filters_to_text(
        _extract_text(run_output.content),
        FilterContext(org_id=ctx.org_id, interface=ctx.interface, db=db),
    )
    metrics = getattr(run_output, "metrics", None)

    return SendMessageResponse(
        id=run_output.run_id or str(uuid.uuid4()),
        session_id=str(session.id),
        agno_session_id=session.agno_session_id,
        role="assistant",
        content=content,
        created_at=datetime.now(timezone.utc),
        metadata=MessageMetadata(
            run_id=run_output.run_id,
            tokens=MessageTokenMetadata(
                input=getattr(metrics, "input_tokens", None) if metrics else None,
                output=getattr(metrics, "output_tokens", None) if metrics else None,
            ) if metrics else None,
            duration_ms=(
                int(getattr(metrics, "duration", 0) * 1000)
                if metrics and getattr(metrics, "duration", None)
                else None
            ),
        ),
    )


# ---------------------------------------------------------------------------
# 9. Stream message (SSE)
# ---------------------------------------------------------------------------


async def _sse_stream(
    agent,
    message: str,
    msg_id: str,
    agno_session_id: str,
    *,
    ctx: "TenantContext",
    db: "AsyncSession",
    org,
    pending_bg_tasks: "list | None" = None,
    gsage_session_id: "uuid.UUID | None" = None,
) -> AsyncIterator[str]:
    """Async generator that yields SSE-formatted frames.

    Retries the LLM call up to ``_LLM_RETRY_ATTEMPTS`` times on transient
    provider errors (503 / Service Unavailable), but only if no content has
    been delivered to the client yet.  Once streaming has started, any error
    yields an ``error`` SSE frame so the frontend can display a message.
    """
    from agno.run.agent import RunEvent

    yield _fmt_sse(
        "message_start",
        {"id": msg_id, "role": "assistant", "session_id": agno_session_id},
    )

    final_metrics: dict = {}
    pending_approval_ids: list[str] = []
    auto_approved_ids: list[str] = []
    paused_run_id: Optional[str] = None
    content_started = False
    retries_left = _LLM_RETRY_ATTEMPTS
    stream_filter = StreamFilter(
        FilterContext(org_id=ctx.org_id, interface=ctx.interface, db=db)
    )

    # Serialize runs against the same Agno session so a concurrent
    # background-tool continuation cannot race this run and overwrite the
    # persisted history snapshot.  Bounded wait — the user gets a clear
    # "busy" message if another run is mid-flight on the same session.
    from src.backend_api.app.services.agno_session_lock import (  # noqa: PLC0415
        LockAcquireError,
        acquire as _acquire_session_lock,
        publish_conversation_updated,
    )

    try:
        _lock_cm = _acquire_session_lock(
            agno_session_id, owner="sse:stream_message"
        )
        _lock_held = False
        try:
            await _lock_cm.__aenter__()
            _lock_held = True
        except LockAcquireError as exc:
            log.warning(
                "SSE: could not acquire session lock for %s: %s",
                agno_session_id, exc,
            )
            yield _fmt_sse(
                "content_delta",
                {"delta": _LLM_SESSION_BUSY_MSG},
            )
            yield _fmt_sse(
                "message_end",
                {"id": msg_id, "metadata": {}, "status": "busy"},
            )
            return

        while True:
            try:
                async for chunk in agent.arun(message, stream=True):
                    event_type = getattr(chunk, "event", None)

                    if event_type == RunEvent.run_content:
                        delta = _extract_text(getattr(chunk, "content", None))
                        if delta:
                            content_started = True
                            emit = await stream_filter.feed(delta)
                            if emit:
                                yield _fmt_sse("content_delta", {"delta": emit})

                    elif event_type == RunEvent.run_paused:
                        # Emit any remaining content from the paused chunk
                        paused_content = _extract_text(getattr(chunk, "content", None))
                        if paused_content:
                            content_started = True
                            emit = await stream_filter.feed(paused_content)
                            if emit:
                                yield _fmt_sse("content_delta", {"delta": emit})

                        paused_run_id = getattr(chunk, "run_id", None)
                        for req in getattr(chunk, "requirements", None) or []:
                            te = getattr(req, "tool_execution", None)
                            approval_id = getattr(te, "approval_id", None) if te else None
                            if approval_id:
                                pending_approval_ids.append(str(approval_id))

                        # Auto-approval pass: resolve flagged tools immediately
                        # and only delegate / notify for the manual remainder.
                        try:
                            auto_ids, manual_ids = await _process_auto_approvals(
                                approval_ids=pending_approval_ids,
                                ctx=ctx,
                                db=db,
                            )
                        except Exception as auto_exc:
                            log.error(
                                "SSE auto-approval processing error: %s",
                                auto_exc, exc_info=True,
                            )
                            auto_ids, manual_ids = [], list(pending_approval_ids)
                        auto_approved_ids.extend(auto_ids)
                        # Replace pending list with the manual subset so the
                        # SSE event / metadata only mentions approvals that
                        # actually require user action.
                        pending_approval_ids = manual_ids

                        # Process approval delegations (same as sync path)
                        try:
                            await _process_approval_delegations(
                                approval_ids=pending_approval_ids,
                                ctx=ctx,
                                db=db,
                                org=org,
                                agno_session_id=agno_session_id,
                                run_id=str(paused_run_id or ""),
                            )
                        except Exception as deleg_exc:
                            log.error(
                                "SSE delegation processing error: %s",
                                deleg_exc,
                                exc_info=True,
                            )

                        # Skip the run_paused SSE event entirely when every
                        # pending approval was auto-resolved — the UI should
                        # not flash an approval banner. The continuation
                        # Celery task will deliver the assistant's next
                        # message through the conversation's SSE channel.
                        if pending_approval_ids:
                            yield _fmt_sse("run_paused", {
                                "pending_approvals": pending_approval_ids,
                                "run_id": paused_run_id,
                            })
                        else:
                            log.info(
                                "SSE: all %d pending approvals were auto-resolved "
                                "(run_id=%s); skipping run_paused emit",
                                len(auto_ids), paused_run_id,
                            )

                    elif event_type == RunEvent.run_error:
                        err_str = str(getattr(chunk, "content", ""))
                        if not content_started and retries_left > 0 and _is_transient_llm_error(err_str):
                            retries_left -= 1
                            delay = _LLM_RETRY_BASE_DELAY_SECONDS * (2 ** (_LLM_RETRY_ATTEMPTS - retries_left - 1))
                            log.warning(
                                "LLM run_error event, retrying in %.1fs (%d left): %s",
                                delay, retries_left, err_str,
                            )
                            # Reset per-attempt state before retry
                            final_metrics = {}
                            pending_approval_ids = []
                            auto_approved_ids = []
                            paused_run_id = None
                            await asyncio.sleep(delay)
                            break  # break inner for-loop → re-enter while
                        # Non-transient or content already started or retries exhausted
                        log.error("SSE run_error: %s", err_str)
                        yield _fmt_sse("content_delta", {"delta": _LLM_UNAVAILABLE_MSG})
                        yield _fmt_sse(
                            "message_end",
                            {"id": msg_id, "metadata": {}, "status": "error"},
                        )
                        return

                    elif event_type == RunEvent.run_completed:
                        m = getattr(chunk, "metrics", None)
                        if m:
                            final_metrics = {
                                "input": getattr(m, "input_tokens", None),
                                "output": getattr(m, "output_tokens", None),
                            }
                else:
                    # for-loop completed without break → stream finished
                    break

                # for-loop was broken (retry) → continue the while-loop
                continue

            except asyncio.CancelledError:
                log.warning("SSE stream cancelled (client disconnected?)")
                if not content_started:
                    yield _fmt_sse("error", {"detail": _LLM_UNAVAILABLE_MSG})
                return

            except Exception as exc:
                if not content_started and retries_left > 0 and _is_transient_llm_error(str(exc)):
                    retries_left -= 1
                    delay = _LLM_RETRY_BASE_DELAY_SECONDS * (2 ** (_LLM_RETRY_ATTEMPTS - retries_left - 1))
                    log.warning(
                        "Transient LLM SSE error, retrying in %.1fs (%d left): %s",
                        delay, retries_left, exc,
                    )
                    # Reset per-attempt state before retry
                    final_metrics = {}
                    pending_approval_ids = []
                    auto_approved_ids = []
                    paused_run_id = None
                    await asyncio.sleep(delay)
                    continue

                log.error("SSE agent stream error: %s", exc, exc_info=True)
                yield _fmt_sse("content_delta", {"delta": _LLM_UNAVAILABLE_MSG})
                yield _fmt_sse(
                    "message_end",
                    {"id": msg_id, "metadata": {}, "status": "error"},
                )
                return

        # Safety net: stream finished but no content was ever delivered.
        # This can happen when run_error is NOT emitted but the LLM returned nothing.
        # Auto-approved runs are also content-less here — the continuation
        # Celery task will deliver the assistant's next message — so do not
        # treat them as an error.
        if not content_started and not pending_approval_ids and not auto_approved_ids:
            log.warning("SSE stream completed with no content — emitting error")
            yield _fmt_sse("content_delta", {"delta": _LLM_UNAVAILABLE_MSG})
            yield _fmt_sse(
                "message_end",
                {"id": msg_id, "metadata": {}, "status": "error"},
            )
            return

        end_metadata: dict = {"tokens": final_metrics}
        if pending_approval_ids:
            end_metadata["pending_approvals"] = pending_approval_ids
            end_metadata["run_id"] = paused_run_id
        if auto_approved_ids:
            # Signal to the frontend that one or more approvals were
            # auto-resolved; a follow-up assistant message is being produced
            # by the continuation Celery task and will arrive via the
            # conversation's messages_updated SSE channel.
            end_metadata["auto_approved"] = auto_approved_ids
            end_metadata["has_active_bg_tasks"] = True

        # Flush any text held back by the response filter (e.g. trailing
        # text that could have been an opening fence).
        tail = await stream_filter.flush()
        if tail:
            yield _fmt_sse("content_delta", {"delta": tail})

        # Detect active (QUEUED/RUNNING) background tasks so the frontend
        # can start polling for new messages.
        if gsage_session_id is not None:
            from src.backend_api.app.services.background_tasks import has_active_bg_tasks
            try:
                if await has_active_bg_tasks(gsage_session_id, db):
                    end_metadata["has_active_bg_tasks"] = True
            except Exception:
                pass  # best-effort; polling not critical

        yield _fmt_sse(
            "message_end",
            {"id": msg_id, "metadata": end_metadata},
        )

        # Mark injected bg tasks as notified after stream completes
        if pending_bg_tasks:
            await _mark_bg_tasks_notified([t.id for t in pending_bg_tasks], db)

        # Notify other clients viewing this conversation (other tabs/devices)
        # so they refetch immediately instead of waiting for the 5s polling
        # cycle.  Best-effort; failures are swallowed inside the helper.
        if gsage_session_id is not None:
            await publish_conversation_updated(
                gsage_session_id, reason="assistant_message"
            )

    finally:
        # Release the session lock if it was acquired (no-op on failure path).
        if _lock_held:  # type: ignore[possibly-unbound]
            try:
                await _lock_cm.__aexit__(None, None, None)  # type: ignore[possibly-unbound]
            except Exception:
                pass
        await _cleanup_agent_mcp(agent)


@stream_router.post(
    "/orgs/{org_id}/chat/conversations/{conv_id}/messages/stream",
    summary="Send a message and receive a streaming SSE reply",
    response_class=StreamingResponse,
)
async def stream_message(
    org_id: uuid.UUID,
    conv_id: uuid.UUID,
    payload: SendMessageRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """Invoke the agent with ``stream=True`` and return SSE frames."""
    ctx.require_permission("agents:run")
    session = await _get_conv_or_404(conv_id, ctx, db)

    org = await _load_org(ctx.org_id, db)
    user = await _load_user(ctx.user_id, db)
    profile_org, profile_user = await load_interface_profiles(
        ctx.org_id, ctx.user_id, ctx.interface, db
    )
    tool_catalog = await _fetch_tool_catalog(ctx, gsage_session_id=session.id)
    agent = build_agent(
        ctx=ctx,
        agent_id=DEFAULT_AGENT_ID,
        session_id=session.agno_session_id,
        org=org,
        user=user,
        interface_profile_org=profile_org,
        interface_profile_user=profile_user,
        gsage_session_id=session.id,
        tool_catalog=tool_catalog,
    )
    msg_id = str(uuid.uuid4())

    # Inject completed background task results that have not been notified yet
    pending_bg_tasks = await _get_pending_bg_notifications(session.id, db)
    effective_message = payload.message
    if pending_bg_tasks:
        bg_block = _build_bg_context_block(pending_bg_tasks)
        effective_message = f"{bg_block}\n\n---\n{payload.message}"

    # Inject attachment metadata so the LLM knows what files are attached.
    attachments = await _resolve_attachments(
        payload.attachment_ids,
        org_id=org_id,
        user_id=ctx.user_id,
        conv_id=conv_id,
        db=db,
    )
    if attachments:
        att_block = _build_attachment_block(attachments)
        effective_message = f"{att_block}\n\n---\n{effective_message}"

    # Inject active department context so the agent knows which dept is selected
    # and can use it directly in tool calls without asking the user.
    if ctx.dept_id is not None:
        dept_name = await _load_dept_name(ctx.dept_id, db)
        dept_block = _build_dept_context_block(ctx.dept_id, dept_name)
        effective_message = f"{dept_block}\n\n---\n{effective_message}"

    # Auto-inject KB hints (saved notes/memories).  Failure is absorbed.
    from src.shared.services.kb_context import prepend_kb_hints

    effective_message = await prepend_kb_hints(
        effective_message,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        dept_id=ctx.dept_id,
    )

    return StreamingResponse(
        _sse_stream(
            agent,
            effective_message,
            msg_id,
            session.agno_session_id,
            ctx=ctx,
            db=db,
            org=org,
            pending_bg_tasks=pending_bg_tasks,
            gsage_session_id=session.id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# 10. Conversation update events (SSE)
# ---------------------------------------------------------------------------
#
# Long-lived SSE channel that pushes a small ``messages_updated`` event
# every time a new assistant/tool message is appended to the conversation
# from OUTSIDE the user's current request — most importantly when a
# background-tool continuation completes in a Celery worker.  The frontend
# uses this to trigger an immediate refetch of the message list instead of
# waiting for the 5 s polling cycle.


async def _conv_events_stream(
    conv_id: uuid.UUID,
) -> AsyncIterator[str]:
    """SSE generator subscribing to Redis pub/sub updates for *conv_id*."""
    from src.backend_api.app.services.agno_session_lock import (  # noqa: PLC0415
        subscribe_conversation_updates,
    )

    # Initial hello so the client connection completes promptly.
    yield _fmt_sse("connected", {"conv_id": str(conv_id)})

    try:
        async for reason in subscribe_conversation_updates(conv_id):
            if not reason:
                # Keep-alive emitted by the subscriber on idle ticks.
                # SSE comments (lines beginning with ``:``) are ignored
                # by EventSource and keep proxies from closing the
                # connection.
                yield ": keep-alive\n\n"
                continue
            yield _fmt_sse("messages_updated", {"reason": reason})
    except asyncio.CancelledError:
        log.debug("conv_events_stream: client disconnected conv=%s", conv_id)
        return


@stream_router.get(
    "/orgs/{org_id}/chat/conversations/{conv_id}/events",
    summary="Subscribe to conversation update events (SSE)",
    response_class=StreamingResponse,
)
async def conversation_events(
    org_id: uuid.UUID,
    conv_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """SSE endpoint that emits ``messages_updated`` events for *conv_id*.

    The frontend uses these events to refetch the message list immediately
    when a background-tool continuation appends a new assistant message
    (instead of polling every 5 s).
    """
    ctx.require_permission("agents:run")
    # Validate ownership — same check as the message endpoints.
    await _get_conv_or_404(conv_id, ctx, db)

    return StreamingResponse(
        _conv_events_stream(conv_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
