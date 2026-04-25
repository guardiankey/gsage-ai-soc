"""gSage AI — Generated file routes.

Routes
------
GET    /orgs/{org_id}/files                  List tool-generated files for the org/user
GET    /orgs/{org_id}/files/{file_id}/download  Download file content (authenticated proxy)
POST   /orgs/{org_id}/files/upload           Upload a document template
DELETE /orgs/{org_id}/files/{file_id}        Soft-delete a template (owner or files:delete:all)
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_tenant_context
from src.backend_api.app.core.tenant import TenantContext
from src.shared.database import get_db
from src.shared.models.generated_file import GSageFile

router = APIRouter()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEMPLATE_ALLOWED_EXTENSIONS = {
    ".md", ".docx", ".xlsx", ".pptx", ".pdf", ".tex", ".zip",
    ".txt", ".csv", ".json", ".yaml", ".yml", ".html", ".xml", ".latex",
}

_ATTACHMENT_ALLOWED_EXTENSIONS = _TEMPLATE_ALLOWED_EXTENSIONS | {
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".log", ".conf", ".ini",
    ".pcap", ".pcapng", ".eml",
    ".exe", ".bin", ".data",
    ".py", ".js", ".ts", ".sh", ".rb", ".go", ".java", ".c", ".cpp", ".h",
}

_ATTACHMENT_SIZE_LIMIT_BYTES = 50 * 1024 * 1024  # 50 MB

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class FileOut(BaseModel):
    """Serialised file record returned by the API."""

    id: uuid.UUID
    tool_name: str
    filename: str
    content_type: str
    size_bytes: int
    description: Optional[str]
    trace_id: Optional[str]
    expires_at: Optional[str]
    purged_at: Optional[str]
    created_at: str
    category: str
    scope: str
    session_id: Optional[uuid.UUID] = None
    dept_id: Optional[uuid.UUID] = None

    model_config = {"from_attributes": True}


class FileListResponse(BaseModel):
    items: List[FileOut]
    total: int
    page: int
    limit: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_to_out(r: GSageFile) -> FileOut:
    return FileOut(
        id=r.id,
        tool_name=r.tool_name,
        filename=r.filename,
        content_type=r.content_type,
        size_bytes=r.size_bytes,
        description=r.description,
        trace_id=r.trace_id,
        expires_at=r.expires_at.isoformat() if r.expires_at else None,
        purged_at=r.purged_at.isoformat() if r.purged_at else None,
        created_at=r.created_at.isoformat(),
        category=r.category,
        scope=r.scope,
        session_id=r.session_id,
        dept_id=r.dept_id,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/orgs/{org_id}/files",
    response_model=FileListResponse,
    summary="List tool-generated files",
)
async def list_files(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    tool_name: Optional[str] = Query(None, description="Filter by tool name"),
    include_purged: bool = Query(False, description="Include already-purged files"),
    category: Optional[str] = Query(None, description="Filter by category: generated | template"),
) -> FileListResponse:
    """List files owned by the current user in this org.

    - Generated files: user sees own files; ``files:read:all`` sees all.
    - Template files: user sees own files + org-scoped templates;
      ``files:read:all`` sees all templates.
    """
    see_all = ctx.has_permission("files:read:all") or ctx.has_permission("*")

    stmt = select(GSageFile).where(GSageFile.org_id == org_id)

    if not see_all:
        if category == "template":
            # User sees: own templates + org-scoped + dept-scoped (if in dept)
            from sqlalchemy import and_, or_
            dept_clause = (
                (GSageFile.scope == "department")
                & (GSageFile.dept_id == ctx.dept_id)
            ) if ctx.dept_id else None
            scope_clauses = [
                GSageFile.user_id == ctx.user_id,
                GSageFile.scope == "organization",
            ]
            if dept_clause is not None:
                scope_clauses.append(dept_clause)
            stmt = stmt.where(or_(*scope_clauses))
        else:
            stmt = stmt.where(GSageFile.user_id == ctx.user_id)

    if tool_name:
        stmt = stmt.where(GSageFile.tool_name == tool_name)

    if category:
        stmt = stmt.where(GSageFile.category == category)

    if not include_purged:
        stmt = stmt.where(GSageFile.purged_at.is_(None))

    # Count total
    from sqlalchemy import func, select as sa_select
    count_stmt = sa_select(func.count()).select_from(stmt.subquery())
    total: int = (await db.execute(count_stmt)).scalar_one()

    # Paginate
    stmt = (
        stmt.order_by(GSageFile.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()

    return FileListResponse(
        items=[_file_to_out(r) for r in rows],
        total=total,
        page=page,
        limit=limit,
    )


@router.get(
    "/orgs/{org_id}/files/{file_id}/download",
    summary="Download a tool-generated file (authenticated proxy)",
    response_class=StreamingResponse,
)
async def download_file(
    org_id: uuid.UUID,
    file_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """Stream file bytes directly to the client.

    Authenticates the request, checks permissions, then proxies the object
    from MinIO to the caller.  MinIO does not need to be externally reachable.

    Raises
    ------
    404
        File not found or does not belong to this org.
    410
        File has been purged — bytes no longer available.
    403
        Caller does not own this file and lacks ``files:read:all``.
    """
    row: Optional[GSageFile] = (
        await db.execute(
            select(GSageFile).where(
                GSageFile.id == file_id,
                GSageFile.org_id == org_id,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")

    see_all = ctx.has_permission("files:read:all") or ctx.has_permission("*")
    if not see_all:
        own_file = str(row.user_id) == str(ctx.user_id)
        org_template = row.category == "template" and row.scope == "organization"
        dept_template = (
            row.category == "template"
            and row.scope == "department"
            and ctx.dept_id is not None
            and str(row.dept_id) == str(ctx.dept_id)
        )
        if not own_file and not org_template and not dept_template:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied — file belongs to another user.",
            )

    if row.purged_at is not None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="File has been purged and is no longer available for download.",
        )

    try:
        from src.shared.services.file_store import get_file_store

        store = get_file_store()
        minio_response = await store.get_object(row.storage_key, category=row.category)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not retrieve file: {exc}",
        ) from exc

    safe_filename = row.filename.replace('"', '\\"')
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_filename}"',
        "Content-Length": str(row.size_bytes),
    }

    def _iter_chunks():
        try:
            for chunk in minio_response.stream(amt=65536):
                yield chunk
        finally:
            minio_response.close()
            minio_response.release_conn()

    return StreamingResponse(
        _iter_chunks(),
        media_type=row.content_type,
        headers=headers,
    )


@router.post(
    "/orgs/{org_id}/files/upload",
    response_model=FileOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document template",
)
async def upload_template(
    org_id: uuid.UUID,
    file: UploadFile,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    description: Optional[str] = Query(None, max_length=500),
    scope: str = Query("user", pattern="^(user|organization|department)$"),
) -> FileOut:
    """Upload a document template to the templates bucket.

    Files are stored with ``category="template"`` and never expire.

    Requires permission ``files:upload`` or ``*``.

    Raises
    ------
    403
        Missing ``files:upload`` permission.
    415
        Unsupported file extension.
    413
        File exceeds the configured size limit.
    """
    ctx.require_permission("files:upload")

    original_name = file.filename or "upload"
    safe_name = Path(original_name).name
    ext = Path(safe_name).suffix.lower()

    if ext not in _TEMPLATE_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type '{ext}'. "
                f"Allowed: {', '.join(sorted(_TEMPLATE_ALLOWED_EXTENSIONS))}"
            ),
        )

    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty file is not allowed.",
        )

    from src.shared.services.file_store import FileStoreError, get_file_store

    store = get_file_store()
    try:
        gfile = await store.upload(
            data=raw,
            filename=safe_name,
            content_type=file.content_type or "application/octet-stream",
            org_id=str(org_id),
            user_id=str(ctx.user_id),
            tool_name="user_upload",
            db=db,
            description=description,
            category="template",
            scope=scope,
            dept_id=str(ctx.dept_id) if scope == "department" and ctx.dept_id else None,
        )
    except FileStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc

    await db.commit()
    await db.refresh(gfile)
    return _file_to_out(gfile)


@router.delete(
    "/orgs/{org_id}/files/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document template",
)
async def delete_template(
    org_id: uuid.UUID,
    file_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Delete a template file from MinIO and remove the DB record.

    Only templates can be deleted through this endpoint (generated files
    are purged automatically by the maintenance task).

    Raises
    ------
    404
        File not found or not in this org.
    403
        Caller does not own the template and lacks ``files:delete:all``.
    409
        File is not a template.
    """
    row: Optional[GSageFile] = (
        await db.execute(
            select(GSageFile).where(
                GSageFile.id == file_id,
                GSageFile.org_id == org_id,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")

    if row.category != "template":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only template files can be deleted via this endpoint.",
        )

    can_delete_all = ctx.has_permission("files:delete:all") or ctx.has_permission("*")
    if not can_delete_all and str(row.user_id) != str(ctx.user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied — template belongs to another user.",
        )

    try:
        from src.shared.services.file_store import get_file_store

        store = get_file_store()
        await store.delete_object(row.storage_key, category="template")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not delete template from storage: {exc}",
        ) from exc

    await db.delete(row)
    await db.commit()


@router.post(
    "/orgs/{org_id}/chat/conversations/{conv_id}/attachments",
    response_model=FileOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file attachment for a chat message",
)
async def upload_attachment(
    org_id: uuid.UUID,
    conv_id: uuid.UUID,
    file: UploadFile,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    description: Optional[str] = Query(None, max_length=500),
) -> FileOut:
    """Upload a file to be attached to a chat message.

    The returned ``id`` must be included in ``attachment_ids`` when sending
    the message.  Files are stored with ``category="attachment"`` and expire
    after the configured TTL (same as generated files).

    Requires permission ``agents:run`` or ``*``.

    Raises
    ------
    403
        Missing ``agents:run`` permission.
    404
        Conversation not found or does not belong to this org.
    415
        Unsupported file extension.
    413
        File exceeds the attachment size limit (50 MB).
    """
    ctx.require_permission("agents:run")

    # Verify conversation belongs to org
    from sqlalchemy import select as _select
    from src.shared.models.tenant_session import GSageTenantSession

    session_row = (
        await db.execute(
            _select(GSageTenantSession).where(
                GSageTenantSession.id == conv_id,
                GSageTenantSession.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if session_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    original_name = file.filename or "upload"
    safe_name = Path(original_name).name
    ext = Path(safe_name).suffix.lower()

    if ext not in _ATTACHMENT_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type '{ext}'. "
                f"Allowed: {', '.join(sorted(_ATTACHMENT_ALLOWED_EXTENSIONS))}"
            ),
        )

    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty file is not allowed.",
        )
    if len(raw) > _ATTACHMENT_SIZE_LIMIT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the 50 MB attachment size limit.",
        )

    from src.shared.services.file_store import FileStoreError, get_file_store

    store = get_file_store()
    try:
        gfile = await store.upload(
            data=raw,
            filename=safe_name,
            content_type=file.content_type or "application/octet-stream",
            org_id=str(org_id),
            user_id=str(ctx.user_id),
            tool_name="chat_attachment",
            db=db,
            description=description,
            category="attachment",
            scope="user",
            session_id=str(conv_id),
        )
    except FileStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc

    await db.commit()
    await db.refresh(gfile)
    return _file_to_out(gfile)
