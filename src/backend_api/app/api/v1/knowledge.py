"""gSage AI — Knowledge base routes.

Routes
------
POST   /orgs/{org_id}/knowledge/search          Search the tenant knowledge base
POST   /orgs/{org_id}/knowledge/content         Add a text document
GET    /orgs/{org_id}/knowledge/content         List stored documents
DELETE /orgs/{org_id}/knowledge/content/{id}    Remove a document
POST   /orgs/{org_id}/knowledge/ingest          Upload a file for async ingestion
GET    /orgs/{org_id}/knowledge/ingest/{job_id} Get ingest job status
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_current_user, get_tenant_context
from src.backend_api.app.core.tenant import TenantContext
from src.backend_api.app.schemas.knowledge import (
    IngestJobStatusResponse,
    IngestJobSubmitResponse,
    KnowledgeContentCreate,
    KnowledgeContentOut,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
)
from src.backend_api.app.schemas.pagination import PaginatedResponse, PaginationParams
from src.backend_api.app.services.agent_factory import get_agno_db
from src.backend_api.app.services.knowledge import build_knowledge, knowledge_linked_to
from src.shared.database import get_db
from src.shared.models.ingest_job import GSageIngestJob, IngestScope, IngestStatus
from src.shared.models.user import GSageUser
from src.shared.models.user_organization import GSageUserOrganization

router = APIRouter()

# Second router mounted at ``/api/kb`` (no org in path) — provides short,
# token-authenticated download URLs that the agent can cite in its answers.
download_router = APIRouter()




# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/orgs/{org_id}/knowledge/search",
    response_model=KnowledgeSearchResponse,
    summary="Search tenant knowledge base",
)
async def search_knowledge(
    org_id: uuid.UUID,
    payload: KnowledgeSearchRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> KnowledgeSearchResponse:
    """Semantic search over the tenant's knowledge base."""
    ctx.require_permission("knowledge:read")

    kb = build_knowledge(org_id)

    try:
        docs = await kb.asearch(
            query=payload.query,
            max_results=payload.max_results,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Knowledge search failed: {exc}",
        ) from exc

    results: list[KnowledgeSearchResult] = []
    for doc in docs or []:
        content: str | None = None
        meta: dict[str, Any] | None = None
        if hasattr(doc, "content"):
            content = doc.content
        if hasattr(doc, "meta_data"):
            meta = doc.meta_data
        results.append(
            KnowledgeSearchResult(
                id=str(getattr(doc, "id", "")),
                name=getattr(doc, "name", None),
                content=content,
                score=getattr(doc, "score", None),
                metadata=meta,
            )
        )

    return KnowledgeSearchResponse(results=results, total=len(results))


@router.post(
    "/orgs/{org_id}/knowledge/content",
    response_model=KnowledgeContentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a document to the tenant knowledge base",
)
async def add_knowledge_content(
    org_id: uuid.UUID,
    payload: KnowledgeContentCreate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> KnowledgeContentOut:
    import hashlib as _hashlib
    import uuid as _uuid_mod

    ctx.require_permission("knowledge:write")

    # Normalize empty description to None — agno excludes empty strings from content hash
    description = payload.description or None

    # Resolve the final text content (from payload or fetched from URL)
    text_content: str
    metadata: dict[str, Any] = dict(payload.metadata or {})

    if payload.content:
        # Content provided directly — use as-is, optionally annotate with URL
        text_content = payload.content
        if payload.url:
            metadata["source_url"] = payload.url
    else:
        # No content provided — fetch from URL
        _MAX_FETCH_BYTES = 5 * 1024 * 1024  # 5 MB
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(30.0),
            ) as client:
                response = await client.get(str(payload.url))
                response.raise_for_status()
                raw_bytes = response.content
                if len(raw_bytes) > _MAX_FETCH_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"URL response exceeds 5 MB limit",
                    )
        except HTTPException:
            raise
        except httpx.TimeoutException:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="URL fetch timed out (30 s limit)",
            )
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"URL returned HTTP {exc.response.status_code}",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Failed to fetch URL: {exc}",
            ) from exc

        content_type = response.headers.get("content-type", "").lower()
        is_plain_text = any(
            ct in content_type
            for ct in ("text/plain", "text/markdown", "text/x-markdown", "application/json")
        )

        if is_plain_text:
            # Plain text / Markdown / JSON — use as-is, no HTML extraction needed
            try:
                text_content = raw_bytes.decode(response.encoding or "utf-8", errors="replace")
            except Exception:
                text_content = raw_bytes.decode("utf-8", errors="replace")
            if not text_content.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Could not extract readable content from the URL",
                )
        else:
            import trafilatura

            extracted = trafilatura.extract(raw_bytes)
            if not extracted:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Could not extract readable content from the URL",
                )
            text_content = extracted
        metadata["source_url"] = str(payload.url)

    knowledge = build_knowledge(org_id)
    await knowledge.ainsert(
        text_content=text_content,
        name=payload.name,
        description=description,
        metadata=metadata if metadata else None,
        upsert=True,
    )

    # Reproduce agno's deterministic content ID to fetch the stored row
    hash_parts = [payload.name]
    if description:
        hash_parts.append(description)
    hash_parts.append("Text")  # FileData(type="Text")
    content_hash = _hashlib.sha256(":".join(hash_parts).encode()).hexdigest()
    content_id = str(_uuid_mod.uuid5(_uuid_mod.NAMESPACE_DNS, content_hash))

    rows, _ = await get_agno_db().get_knowledge_contents(
        linked_to=knowledge_linked_to(org_id)
    )
    stored = next((r for r in rows if str(r.id) == content_id), None)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store knowledge content",
        )
    return KnowledgeContentOut(**stored.model_dump())


@router.get(
    "/orgs/{org_id}/knowledge/content",
    response_model=PaginatedResponse[KnowledgeContentOut],
    summary="List documents in the tenant knowledge base",
)
async def list_knowledge_content(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    pagination: Annotated[PaginationParams, Depends()],
) -> PaginatedResponse[KnowledgeContentOut]:
    ctx.require_permission("knowledge:read")

    rows, total = await get_agno_db().get_knowledge_contents(
        limit=pagination.limit,
        page=pagination.page,
        linked_to=knowledge_linked_to(org_id),
    )
    items = [KnowledgeContentOut(**r.model_dump()) for r in rows]
    return PaginatedResponse.build(items, total=total, pagination=pagination)


@router.delete(
    "/orgs/{org_id}/knowledge/content/{content_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document from the tenant knowledge base",
)
async def delete_knowledge_content(
    org_id: uuid.UUID,
    content_id: str,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> None:
    ctx.require_permission("knowledge:delete")

    # Verify the document belongs to this tenant
    rows, _ = await get_agno_db().get_knowledge_contents(linked_to=knowledge_linked_to(org_id))
    owned_ids = {r.id for r in rows}
    if content_id not in owned_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    knowledge = build_knowledge(org_id)
    await knowledge.aremove_content_by_id(content_id)


# ---------------------------------------------------------------------------
# Document ingest (async Celery pipeline)
# ---------------------------------------------------------------------------

_ALLOWED_EXTENSIONS = {
    # Documents (MarkItDown converts directly)
    ".pdf", ".docx", ".txt", ".md", ".xlsx", ".pptx", ".csv",
    ".html", ".htm", ".json", ".xml", ".eml",
    # Archives (contents extracted and ingested per inner file)
    ".zip", ".tar", ".gz", ".tar.gz", ".tar.bz2", ".tar.xz",
}
_ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tar.gz", ".tar.bz2", ".tar.xz"}
_MAX_FILE_BYTES = 10 * 1024 * 1024    # 10 MB — regular documents
_MAX_ARCHIVE_BYTES = 50 * 1024 * 1024  # 50 MB — archives
_INGEST_BASE_DIR = Path(os.environ.get("INGEST_TMP_DIR", "/data/ingest"))


def _get_effective_ext(filename: str) -> str:
    """Return the effective extension, handling double suffixes like .tar.gz."""
    p = Path(filename)
    if len(p.suffixes) >= 2:
        double = "".join(p.suffixes[-2:]).lower()
        if double in {".tar.gz", ".tar.bz2", ".tar.xz"}:
            return double
    return p.suffix.lower()


@router.post(
    "/orgs/{org_id}/knowledge/ingest",
    response_model=IngestJobSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document for async ingestion into the knowledge base",
)
async def ingest_document(
    org_id: uuid.UUID,
    file: UploadFile,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
    # ``scope`` is sent as a multipart form field by the web client alongside
    # the file. Also accepted as a query string for backwards compatibility
    # with CLI clients — FastAPI resolves Form before falling back is not
    # automatic, so we explicitly declare it as Form here.
    scope: str = Form(IngestScope.ORG, pattern="^(org|user|dept)$"),
) -> IngestJobSubmitResponse:
    """Accept a document file and enqueue it for async processing.

    The file is saved to a shared Docker volume and a Celery task is dispatched
    to convert + chunk + embed it into Weaviate.  The caller receives a job_id
    that can be polled via ``GET /knowledge/ingest/{job_id}``.
    """
    ctx.require_permission("knowledge:write")

    if scope == IngestScope.DEPT and ctx.dept_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Department scope requires an active department context. Switch to a department first.",
        )

    # --- Validate filename & extension -----------------------------------------
    original_name = file.filename or "upload"
    # Strip any path traversal characters
    safe_name = Path(original_name).name
    ext = _get_effective_ext(safe_name)
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
        )

    # --- Read file and validate size -------------------------------------------
    raw = await file.read()
    file_size = len(raw)
    size_limit = _MAX_ARCHIVE_BYTES if ext in _ARCHIVE_EXTENSIONS else _MAX_FILE_BYTES
    limit_label = f"{size_limit // (1024 * 1024)} MB"
    if file_size > size_limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {limit_label} limit ({file_size} bytes received)",
        )
    if file_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty file is not allowed",
        )

    content_type = file.content_type or "application/octet-stream"

    # --- Persist job row --------------------------------------------------------
    job = GSageIngestJob(
        org_id=org_id,
        user_id=ctx.user_id,
        dept_id=ctx.dept_id if scope == IngestScope.DEPT else None,
        scope=scope,
        original_filename=safe_name,
        file_size=file_size,
        status=IngestStatus.QUEUED,
    )
    db.add(job)
    await db.flush()  # populate job.id before saving file
    await db.commit()
    await db.refresh(job)

    # --- Upload original file to MinIO (kb-originals bucket) -------------------
    try:
        from src.shared.services.file_store import get_file_store  # noqa: PLC0415

        store = get_file_store()
        storage_key = await store.upload_kb_original(
            data=raw,
            filename=safe_name,
            content_type=content_type,
            org_id=str(org_id),
            job_id=str(job.id),
        )
        job.storage_key = storage_key
        db.add(job)
        await db.commit()
    except Exception as exc:
        # Non-fatal: ingest processing continues even if original backup fails
        import logging as _logging  # noqa: PLC0415
        _logging.getLogger(__name__).warning(
            "ingest: failed to upload original to MinIO for job %s: %s", job.id, exc
        )

    # --- Save file to shared volume --------------------------------------------
    job_dir = _INGEST_BASE_DIR / str(job.id)
    job_dir.mkdir(parents=True, exist_ok=True)
    dest_path = job_dir / safe_name
    dest_path.write_bytes(raw)

    # --- Dispatch Celery task --------------------------------------------------
    from src.backend_api.app.tasks.ingest import ingest_document_task  # noqa: PLC0415

    ingest_document_task.apply_async(
        kwargs={
            "job_id": str(job.id),
            "org_id": str(org_id),
            "user_id": str(ctx.user_id),
            "filepath": str(dest_path),
            "scope": scope,
            "dept_id": str(ctx.dept_id) if scope == IngestScope.DEPT and ctx.dept_id else None,
        },
        queue="knowledge",
    )

    return IngestJobSubmitResponse(
        job_id=str(job.id),
        status=job.status,
        filename=job.original_filename,
        scope=job.scope,
    )


@router.get(
    "/orgs/{org_id}/knowledge/ingest",
    response_model=PaginatedResponse[IngestJobStatusResponse],
    summary="List ingest jobs for this organisation",
)
async def list_ingest_jobs(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
    pagination: Annotated[PaginationParams, Depends()] = ...,  # type: ignore[assignment]
) -> PaginatedResponse[IngestJobStatusResponse]:
    """Return a paginated list of ingest jobs for the organisation."""
    ctx.require_permission("knowledge:read")

    from sqlalchemy import func  # noqa: PLC0415

    base_stmt = select(GSageIngestJob).where(GSageIngestJob.org_id == org_id)
    total: int = (
        await db.execute(select(func.count()).select_from(base_stmt.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base_stmt.order_by(GSageIngestJob.created_at.desc())
            .offset((pagination.page - 1) * pagination.limit)
            .limit(pagination.limit)
        )
    ).scalars().all()

    items = [
        IngestJobStatusResponse(
            job_id=str(r.id),
            status=r.status,
            filename=r.original_filename,
            scope=r.scope,
            file_size=r.file_size,
            chunks_stored=r.chunks_stored,
            error_message=r.error_message,
            storage_key=r.storage_key,
            created_at=r.created_at.isoformat() if r.created_at else None,
            updated_at=r.updated_at.isoformat() if r.updated_at else None,
        )
        for r in rows
    ]
    return PaginatedResponse.build(items, total=total, pagination=pagination)


@router.get(
    "/orgs/{org_id}/knowledge/ingest/{job_id}",
    response_model=IngestJobStatusResponse,
    summary="Get the status of a document ingest job",
)
async def get_ingest_job_status(
    org_id: uuid.UUID,
    job_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
) -> IngestJobStatusResponse:
    ctx.require_permission("knowledge:read")

    result = await db.execute(
        select(GSageIngestJob).where(
            GSageIngestJob.id == job_id,
            GSageIngestJob.org_id == org_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingest job not found")

    return IngestJobStatusResponse(
        job_id=str(job.id),
        status=job.status,
        filename=job.original_filename,
        scope=job.scope,
        file_size=job.file_size,
        chunks_stored=job.chunks_stored,
        error_message=job.error_message,
        storage_key=job.storage_key,
        created_at=job.created_at.isoformat() if job.created_at else None,
        updated_at=job.updated_at.isoformat() if job.updated_at else None,
    )


@router.get(
    "/orgs/{org_id}/knowledge/ingest/{job_id}/download",
    summary="Download the original file for an ingest job",
    response_class=StreamingResponse,
)
async def download_ingest_original(
    org_id: uuid.UUID,
    job_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream the original file that was uploaded for an ingest job.

    Only available for files ingested after the kb-originals feature was
    deployed (``storage_key`` is non-null).
    """
    ctx.require_permission("knowledge:read")

    result = await db.execute(
        select(GSageIngestJob).where(
            GSageIngestJob.id == job_id,
            GSageIngestJob.org_id == org_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingest job not found")

    return await _stream_ingest_original(job)


async def _stream_ingest_original(job: GSageIngestJob) -> StreamingResponse:
    """Stream the MinIO object backing *job* as an attachment.

    Shared helper used by both the canonical org-scoped route and the short
    ``/api/kb/download/{job_id}`` alias consumed by the chat UI.
    """
    if not job.storage_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Original file not available — this document was ingested before download support was added.",
        )

    try:
        from src.shared.services.file_store import get_file_store  # noqa: PLC0415

        store = get_file_store()
        minio_response = await store.get_kb_original(job.storage_key)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not retrieve file: {exc}",
        ) from exc

    safe_filename = job.original_filename.replace('"', '\\"')
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_filename}"',
        "Content-Length": str(job.file_size),
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
        media_type="application/octet-stream",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Short download alias — /api/kb/download/{job_id}
# ---------------------------------------------------------------------------


@download_router.get(
    "/kb/download/{job_id}",
    summary="Short alias to download the original file for an ingest job",
    response_class=StreamingResponse,
)
async def download_ingest_original_alias(
    job_id: uuid.UUID,
    user: Annotated[GSageUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Token-authenticated short URL used by the chat UI.

    Auth: JWT Bearer or user-bound API key (``get_current_user``).  The job's
    ``org_id`` is looked up from the DB and verified against the caller's
    active org memberships, so the same authorization guarantees as the
    canonical org-scoped route apply — we simply avoid forcing the LLM to
    emit the org UUID in every citation.
    """
    result = await db.execute(
        select(GSageIngestJob).where(GSageIngestJob.id == job_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingest job not found")

    # Verify the caller is an active member of the job's organization.
    mem_result = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user.id,
            GSageUserOrganization.org_id == job.org_id,
            GSageUserOrganization.is_active == True,  # noqa: E712
        )
    )
    if mem_result.scalar_one_or_none() is None:
        # 404 instead of 403 to avoid leaking job existence across orgs.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingest job not found")

    return await _stream_ingest_original(job)
