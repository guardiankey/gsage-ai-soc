"""gSage AI — Document ingest Celery task.

Tasks
-----
ingest_document_task
    Convert an uploaded file with MarkItDown, chunk the text, and store each
    chunk in the org's Weaviate knowledge base with the appropriate metadata.
    Updates the ``GSageIngestJob`` row (status, chunks_stored, error_message).
    Deletes the temporary file on completion or failure.

load_default_knowledge_task
    Read all ``*.md`` and ``*.txt`` files under ``knowledge_base/default/`` and
    ingest them as SYSTEM-source knowledge for the given org.  Intended to be
    called once on org creation.

Both tasks run on the ``knowledge`` Celery queue.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, cast

from src.backend_api.app.celery_app import celery_app

log = logging.getLogger(__name__)

# Characters per chunk. Read from settings so deployments can tune it to the
# embedding model's context window. Default 2500 chars ≈ 600-800 tokens,
# comfortably below the 8192 token ctx of nomic-embed-ctx8k even for dense
# text, tables, base64, etc.
from src.shared.config.settings import get_settings as _get_settings  # noqa: E402

_CHUNK_SIZE = _get_settings().ingest_chunk_size

# Default knowledge base directory (relative to project root)
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_KB_DIR = _PROJECT_ROOT / "knowledge_base" / "default"
_DOCS_DIR = _PROJECT_ROOT / "docs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chunk_text(text: str, size: int = _CHUNK_SIZE) -> list[str]:
    """Split *text* into non-overlapping chunks of at most *size* characters."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += size
    return chunks or [""]


# ---------------------------------------------------------------------------
# Archive extraction helpers
# ---------------------------------------------------------------------------

_ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tar.gz", ".tar.bz2", ".tar.xz"}
_INNER_ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".txt", ".md", ".xlsx", ".pptx", ".csv",
    ".html", ".htm", ".json", ".xml", ".eml",
}
_MAX_EXTRACTED_BYTES = 100 * 1024 * 1024  # 100 MB total uncompressed
_MAX_EXTRACTED_FILES = 200


def _get_effective_ext(filename: str) -> str:
    """Return the effective extension, handling double suffixes like .tar.gz."""
    p = Path(filename)
    if len(p.suffixes) >= 2:
        double = "".join(p.suffixes[-2:]).lower()
        if double in {".tar.gz", ".tar.bz2", ".tar.xz"}:
            return double
    return p.suffix.lower()


def _is_safe_member_path(member_path: str) -> bool:
    """Reject path traversal sequences and absolute paths."""
    parts = Path(member_path).parts
    return bool(parts) and all(p != ".." and not p.startswith("/") for p in parts)


def _safe_extract_zip(src: Path, dest: Path) -> list[Path]:
    import zipfile  # stdlib

    extracted: list[Path] = []
    total_bytes = 0
    with zipfile.ZipFile(src, "r") as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if len(members) > _MAX_EXTRACTED_FILES:
            raise ValueError(
                f"Archive contains {len(members)} entries; limit is {_MAX_EXTRACTED_FILES}"
            )
        for member in members:
            if not _is_safe_member_path(member.filename):
                log.warning("ZIP: skipping unsafe path: %s", member.filename)
                continue
            if Path(member.filename).suffix.lower() not in _INNER_ALLOWED_EXTENSIONS:
                continue
            total_bytes += member.file_size
            if total_bytes > _MAX_EXTRACTED_BYTES:
                raise ValueError(
                    f"Archive exceeds {_MAX_EXTRACTED_BYTES // (1024 * 1024)} MB "
                    "uncompressed limit (possible zip bomb)"
                )
            safe_path = dest.joinpath(*Path(member.filename).parts)
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            safe_path.write_bytes(zf.read(member))
            extracted.append(safe_path)
    return extracted


def _safe_extract_tar(src: Path, dest: Path) -> list[Path]:
    import tarfile  # stdlib
    from typing import Literal

    mode_map: dict[str, Literal["r", "r:*", "r:", "r:gz", "r:bz2", "r:xz"]] = {
        ".tar.gz": "r:gz",
        ".tar.bz2": "r:bz2",
        ".tar.xz": "r:xz",
        ".tar": "r:",
    }
    mode: Literal["r", "r:*", "r:", "r:gz", "r:bz2", "r:xz"] = mode_map.get(
        _get_effective_ext(src.name), "r:*"
    )
    extracted: list[Path] = []
    total_bytes = 0
    with tarfile.open(src, mode) as tf:
        members = tf.getmembers()
        if len(members) > _MAX_EXTRACTED_FILES:
            raise ValueError(
                f"Archive contains {len(members)} entries; limit is {_MAX_EXTRACTED_FILES}"
            )
        for member in members:
            if not member.isfile():
                continue
            if member.issym() or member.islnk():
                log.warning("TAR: skipping symlink/hardlink: %s", member.name)
                continue
            if not _is_safe_member_path(member.name):
                log.warning("TAR: skipping unsafe path: %s", member.name)
                continue
            if Path(member.name).suffix.lower() not in _INNER_ALLOWED_EXTENSIONS:
                continue
            total_bytes += member.size
            if total_bytes > _MAX_EXTRACTED_BYTES:
                raise ValueError(
                    f"Archive exceeds {_MAX_EXTRACTED_BYTES // (1024 * 1024)} MB "
                    "uncompressed limit (possible tar bomb)"
                )
            fobj = tf.extractfile(member)
            if fobj is None:
                continue
            safe_path = dest.joinpath(*Path(member.name).parts)
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            safe_path.write_bytes(fobj.read())
            extracted.append(safe_path)
    return extracted


def _safe_extract_gz(src: Path, dest: Path) -> list[Path]:
    import gzip  # stdlib

    inner_name = src.stem  # e.g. "report.txt" from "report.txt.gz"
    if Path(inner_name).suffix.lower() not in _INNER_ALLOWED_EXTENSIONS:
        log.warning(
            "GZ: inner file '%s' has unsupported extension — skipping", inner_name
        )
        return []
    dest_file = dest / inner_name
    with gzip.open(src, "rb") as gz_in:
        # Read one byte past the limit to detect oversized files
        data = gz_in.read(_MAX_EXTRACTED_BYTES + 1)
    if len(data) > _MAX_EXTRACTED_BYTES:
        raise ValueError(
            f"Decompressed size exceeds {_MAX_EXTRACTED_BYTES // (1024 * 1024)} MB limit"
        )
    dest_file.write_bytes(data)
    return [dest_file]


def _expand_archive(src: Path, dest: Path) -> list[Path]:
    """Extract archive *src* into *dest* and return valid inner files."""
    dest.mkdir(parents=True, exist_ok=True)
    ext = _get_effective_ext(src.name)
    if ext == ".zip":
        return _safe_extract_zip(src, dest)
    if ext in {".tar.gz", ".tar.bz2", ".tar.xz", ".tar"}:
        return _safe_extract_tar(src, dest)
    if ext == ".gz":
        return _safe_extract_gz(src, dest)
    raise ValueError(f"Unsupported archive extension: {ext!r}")


async def _update_job(session_maker, job_id: str, **kwargs) -> None:
    """Async helper: apply *kwargs* as column updates to a GSageIngestJob row."""
    from sqlalchemy import update

    from src.shared.models.ingest_job import GSageIngestJob

    async with session_maker() as session:
        await session.execute(
            update(GSageIngestJob)
            .where(GSageIngestJob.id == uuid.UUID(job_id))
            .values(**kwargs)
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Main task
# ---------------------------------------------------------------------------


@celery_app.task(
    name="src.backend_api.app.tasks.ingest.ingest_document_task",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def ingest_document_task(
    self,
    *,
    job_id: str,
    org_id: str,
    user_id: str,
    filepath: str,
    scope: str,
    dept_id: str | None = None,
) -> dict:
    """Convert, chunk, and embed a document file into Weaviate.

    Parameters
    ----------
    job_id:
        UUID of the ``GSageIngestJob`` row to update.
    org_id:
        Organisation UUID string (tenant boundary).
    user_id:
        User UUID string (stored in Weaviate metadata when scope == "user").
    filepath:
        Absolute path to the file saved in the shared Docker volume.
    scope:
        ``"org"``, ``"user"``, or ``"dept"``.
    dept_id:
        Department UUID string (stored in Weaviate metadata when scope == "dept").
    """
    import uuid as _uuid

    from markitdown import MarkItDown
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.backend_api.app.services.knowledge import build_knowledge, evict_knowledge_cache
    from src.shared.config.settings import get_settings
    from src.shared.models.ingest_job import IngestStatus
    from src.shared.models.knowledge_base import GSageKnowledgeSource

    filepath_path = Path(filepath)
    tmp_dir = filepath_path.parent

    async def _run() -> int:
        # Create a fresh engine for this task invocation.
        # Using the module-level cached engine causes "Future attached to a
        # different loop" because asyncio.run() creates a new event loop each
        # time while the pool retains connections from the previous loop.
        settings = get_settings()
        engine = create_async_engine(
            settings.database_url, echo=False, pool_pre_ping=True
        )
        session_maker = async_sessionmaker(engine, expire_on_commit=False)

        org_uuid = _uuid.UUID(org_id)  # defined here so finally can reference it

        try:
            # 1 — Mark job as processing
            await _update_job(session_maker, job_id, status=IngestStatus.PROCESSING)

            # 2 — Determine files to process (expand archives if needed)
            effective_ext = _get_effective_ext(filepath_path.name)
            is_archive = effective_ext in _ARCHIVE_EXTENSIONS
            extract_dir = tmp_dir / f"extracted_{filepath_path.stem}"

            if is_archive:
                try:
                    inner_files = _expand_archive(filepath_path, extract_dir)
                except Exception as exc:
                    raise RuntimeError(f"Archive extraction failed: {exc}") from exc
                if not inner_files:
                    raise ValueError(
                        "Archive contained no supported files after extraction. "
                        f"Supported inner formats: {', '.join(sorted(_INNER_ALLOWED_EXTENSIONS))}"
                    )
            else:
                inner_files = [filepath_path]

            # 3 — Build knowledge instance.
            # Evict cache first: each asyncio.run() creates a new event loop,
            # so any cached async clients from a previous task would fail.
            # Use a fresh AsyncPostgresDb tied to this task's engine to avoid
            # "Future attached to a different loop" errors.
            from agno.db.postgres.async_postgres import AsyncPostgresDb
            agno_contents_db = AsyncPostgresDb(db_engine=engine)
            evict_knowledge_cache(org_uuid)
            knowledge = build_knowledge(org_uuid, contents_db=agno_contents_db)
            assert knowledge.vector_db is not None, (
                "build_knowledge must always provide a vector_db"
            )
            embedder = knowledge.vector_db.embedder

            # 4 — For each file: convert → chunk → embed → store
            md = MarkItDown()
            original_filename = filepath_path.name
            total_stored = 0
            total_failed_embed = 0

            for inner_path in inner_files:
                inner_name = inner_path.name

                try:
                    conv_result = md.convert(str(inner_path))
                    text_content: str = conv_result.text_content or ""
                except Exception as exc:
                    if not is_archive:
                        raise RuntimeError(f"MarkItDown conversion failed: {exc}") from exc
                    log.warning(
                        "ingest_document_task: conversion failed for '%s' in job=%s: %s",
                        inner_name, job_id, exc,
                    )
                    continue

                if not text_content.strip():
                    if not is_archive:
                        raise ValueError("Converted document produced no text content")
                    log.warning(
                        "ingest_document_task: no text from '%s' in job=%s — skipping",
                        inner_name, job_id,
                    )
                    continue

                chunks = _chunk_text(text_content)
                file_failed = 0

                for i, chunk in enumerate(chunks):
                    if not chunk.strip():
                        continue

                    # Pre-validate embedding before sending to Weaviate.
                    embedding = await embedder.async_get_embedding(chunk)
                    if not embedding:
                        log.warning(
                            "ingest_document_task: empty embedding for job=%s file=%s chunk=%d/%d — skipping",
                            job_id, inner_name, i + 1, len(chunks),
                        )
                        file_failed += 1
                        continue

                    meta: dict = {
                        "source": GSageKnowledgeSource.DOCUMENT_UPLOAD.value,
                        "scope": scope,
                        "original_filename": original_filename,
                        "chunk_index": i,
                        "job_id": job_id,
                    }
                    if is_archive:
                        meta["inner_filename"] = inner_name
                    if scope == "user":
                        meta["user_id"] = user_id
                    if scope == "dept" and dept_id:
                        meta["dept_id"] = dept_id

                    await knowledge.ainsert(
                        text_content=chunk,
                        name=f"{inner_name} [chunk {i + 1}/{len(chunks)}]",
                        metadata=meta,
                        upsert=True,
                    )
                    total_stored += 1

                total_failed_embed += file_failed

                # For single-file uploads, any embed failure is fatal
                if not is_archive and file_failed:
                    raise RuntimeError(
                        f"Embedding failed for {file_failed}/{len(chunks)} chunk(s). "
                        "Check that the Ollama embedding model is running and accessible."
                    )

            if total_stored == 0:
                raise RuntimeError(
                    "No chunks were stored. "
                    + (
                        f"Archive had {len(inner_files)} file(s) but none produced embeddable content."
                        if is_archive
                        else "Document produced no embeddable content."
                    )
                )

            # 5 — Mark completed (same event loop — no loop mismatch)
            await _update_job(
                session_maker,
                job_id,
                status=IngestStatus.COMPLETED,
                chunks_stored=total_stored,
            )
            log.info(
                "ingest_document_task: job=%s org=%s file=%s inner_files=%d chunks=%d",
                job_id, org_id, filepath_path.name, len(inner_files), total_stored,
            )
            return total_stored

        except Exception as exc:
            log.error(
                "ingest_document_task failed: job=%s error=%s", job_id, exc, exc_info=True
            )
            try:
                await _update_job(
                    session_maker,
                    job_id,
                    status=IngestStatus.FAILED,
                    error_message=str(exc)[:2000],
                )
            except Exception:
                log.warning("Could not update job status to FAILED for job=%s", job_id)
            raise

        finally:
            # Return DB connections to the pool and close the engine
            await engine.dispose()
            # Evict cache so the next task always creates fresh async clients
            evict_knowledge_cache(org_uuid)

    try:
        chunks_stored = asyncio.run(_run())
        return {"status": "completed", "chunks_stored": chunks_stored}

    except Exception as exc:
        return {"status": "failed", "error": str(exc)}

    finally:
        # Always clean up: extracted directory (if archive) + uploaded file + job dir
        try:
            extract_dir = tmp_dir / f"extracted_{filepath_path.stem}"
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
            if filepath_path.exists():
                filepath_path.unlink()
            if tmp_dir.exists() and not any(tmp_dir.iterdir()):
                tmp_dir.rmdir()
        except OSError as cleanup_exc:
            log.warning("Could not clean up temp file %s: %s", filepath, cleanup_exc)


# ---------------------------------------------------------------------------
# URL ingest task — download → save to shared volume → reuse document pipeline
# ---------------------------------------------------------------------------

# Allowed extensions for URL-sourced files (subset of _ALLOWED_EXTENSIONS in
# the API layer). Archives are intentionally NOT supported via URL to avoid
# zip-bomb-from-URL surface.
_URL_ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".txt", ".md", ".xlsx", ".pptx", ".csv",
    ".html", ".htm", ".json", ".xml", ".eml",
}

# Map Content-Type → extension (covers common server responses).
_CONTENT_TYPE_TO_EXT = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/csv": ".csv",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/x-markdown": ".md",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "message/rfc822": ".eml",
}

_URL_MAX_BYTES = 10 * 1024 * 1024  # 10 MB — same as regular file uploads


def _extract_filename_from_disposition(header: str | None) -> str | None:
    """Best-effort parse of Content-Disposition `filename=` parameter."""
    if not header:
        return None
    import re

    # RFC 5987 (filename*=) takes precedence over filename=
    m = re.search(r"filename\*\s*=\s*(?:[^']*''\s*)?([^;\s]+)", header, flags=re.IGNORECASE)
    if not m:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', header, flags=re.IGNORECASE)
    if not m:
        return None
    from urllib.parse import unquote

    return unquote(m.group(1).strip())


@celery_app.task(
    name="src.backend_api.app.tasks.ingest.ingest_url_task",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def ingest_url_task(
    self,
    *,
    job_id: str,
    org_id: str,
    user_id: str,
    source_url: str,
    name: str,
    scope: str,
    dept_id: str | None = None,
) -> dict:
    """Download a URL into the shared ingest volume, then dispatch the
    standard ``ingest_document_task`` pipeline.

    Releases the API request immediately — all I/O (HTTP fetch, content-type
    detection, MarkItDown conversion, embedding) happens here on the
    ``knowledge`` Celery queue.
    """
    import asyncio as _asyncio
    import mimetypes
    import os as _os
    from urllib.parse import urlparse, unquote

    import httpx as _httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.shared.config.settings import get_settings
    from src.shared.models.ingest_job import IngestStatus

    base_dir = Path(_os.environ.get("INGEST_TMP_DIR", "/data/ingest"))
    job_dir = base_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dest_path: Path | None = None

    async def _download() -> tuple[Path, int, str]:
        async with _httpx.AsyncClient(
            follow_redirects=True,
            timeout=_httpx.Timeout(60.0),
        ) as client:
            response = await client.get(source_url)
            response.raise_for_status()
            raw_bytes = response.content
            if len(raw_bytes) > _URL_MAX_BYTES:
                raise ValueError(
                    f"URL response exceeds {_URL_MAX_BYTES // (1024 * 1024)} MB limit"
                )

            # 1) Try Content-Disposition filename
            disp_name = _extract_filename_from_disposition(
                response.headers.get("content-disposition")
            )
            ext = ""
            if disp_name:
                ext = Path(disp_name).suffix.lower()

            # 2) Fall back to Content-Type
            if ext not in _URL_ALLOWED_EXTENSIONS:
                ct_full = (response.headers.get("content-type") or "").lower()
                ct = ct_full.split(";", 1)[0].strip()
                ext = _CONTENT_TYPE_TO_EXT.get(ct, "")
                if not ext:
                    guessed = mimetypes.guess_extension(ct) or ""
                    ext = guessed.lower()

            # 3) Fall back to URL path
            if ext not in _URL_ALLOWED_EXTENSIONS:
                parsed = urlparse(source_url)
                path_ext = Path(unquote(parsed.path)).suffix.lower()
                if path_ext in _URL_ALLOWED_EXTENSIONS:
                    ext = path_ext

            # 4) Default to .html (MarkItDown handles HTML well)
            if ext not in _URL_ALLOWED_EXTENSIONS:
                ext = ".html"

            # Build a safe filename based on the user-provided name
            safe_stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip(".") or "document"
            safe_stem = safe_stem[:200]  # cap length
            target = job_dir / f"{safe_stem}{ext}"
            target.write_bytes(raw_bytes)
            return target, len(raw_bytes), f"{safe_stem}{ext}"

    async def _run() -> str:
        nonlocal dest_path
        settings = get_settings()
        engine = create_async_engine(
            settings.database_url, echo=False, pool_pre_ping=True
        )
        session_maker = async_sessionmaker(engine, expire_on_commit=False)

        try:
            await _update_job(session_maker, job_id, status=IngestStatus.PROCESSING)
            target, size, filename = await _download()
            dest_path = target
            await _update_job(
                session_maker,
                job_id,
                original_filename=filename,
                file_size=size,
                # Stay PROCESSING — ingest_document_task will set its own status.
            )
            return str(target)
        finally:
            await engine.dispose()

    try:
        filepath_str = _asyncio.run(_run())
    except Exception as exc:
        log.error(
            "ingest_url_task failed during download: job=%s url=%s error=%s",
            job_id, source_url, exc, exc_info=True,
        )

        async def _mark_failed() -> None:
            settings = get_settings()
            engine = create_async_engine(
                settings.database_url, echo=False, pool_pre_ping=True
            )
            session_maker = async_sessionmaker(engine, expire_on_commit=False)
            try:
                await _update_job(
                    session_maker,
                    job_id,
                    status=IngestStatus.FAILED,
                    error_message=f"URL fetch failed: {exc}"[:2000],
                )
            finally:
                await engine.dispose()

        try:
            _asyncio.run(_mark_failed())
        except Exception:
            log.warning("Could not update job status to FAILED for job=%s", job_id)
        # Best-effort cleanup of the partial download dir
        try:
            if dest_path and dest_path.exists():
                dest_path.unlink()
            if job_dir.exists() and not any(job_dir.iterdir()):
                job_dir.rmdir()
        except OSError:
            pass
        return {"status": "failed", "error": str(exc)}

    # Hand off to the standard document pipeline (it will mark COMPLETED).
    cast(Any, ingest_document_task).apply_async(
        kwargs={
            "job_id": job_id,
            "org_id": org_id,
            "user_id": user_id,
            "filepath": filepath_str,
            "scope": scope,
            "dept_id": dept_id,
        },
        queue="knowledge",
    )
    return {"status": "downloaded", "filepath": filepath_str}


# ---------------------------------------------------------------------------
# Default knowledge loader
# ---------------------------------------------------------------------------


@celery_app.task(
    name="src.backend_api.app.tasks.ingest.load_default_knowledge_task",
)
def load_default_knowledge_task(*, org_id: str) -> dict:
    """Load default system knowledge for a newly created org.

    Scans two directories recursively for ``*.md`` and ``*.txt`` files:

    * ``knowledge_base/default/`` — curated KB entries
    * ``docs/`` — architecture and developer documentation

    Each chunk is stored with ``source=SYSTEM`` in the org's Weaviate
    collection so agents can answer questions about the platform itself.
    """
    import uuid as _uuid

    from src.backend_api.app.services.knowledge import build_knowledge, evict_knowledge_cache
    from src.shared.models.knowledge_base import GSageKnowledgeSource

    # Build the file list from all configured source directories
    _SOURCE_DIRS = [
        (_DEFAULT_KB_DIR, "knowledge_base/default"),
        (_DOCS_DIR, "docs"),
    ]

    # List of (Path, root_label) tuples — root_label used in metadata
    files: list[tuple[Path, str]] = []
    for source_dir, label in _SOURCE_DIRS:
        if not source_dir.is_dir():
            log.info(
                "load_default_knowledge_task: source dir %s not found — skipping", source_dir
            )
            continue
        for fpath in sorted(source_dir.rglob("*.md")) + sorted(source_dir.rglob("*.txt")):
            files.append((fpath, label))

    if not files:
        log.info("load_default_knowledge_task: org=%s — no default files found", org_id)
        return {"status": "skipped", "reason": "no files"}

    org_uuid = _uuid.UUID(org_id)

    async def _run() -> int:
        # Evict cached Knowledge so fresh async clients are created for this
        # event loop (prevents "Future attached to different loop" errors on
        # OllamaEmbedder and Weaviate async clients).
        evict_knowledge_cache(org_uuid)
        knowledge = build_knowledge(org_uuid)
        assert knowledge.vector_db is not None, (
            "build_knowledge must always provide a vector_db"
        )
        embedder = knowledge.vector_db.embedder

        try:
            stored = 0
            for fpath, label in files:
                text = fpath.read_text(encoding="utf-8", errors="replace")
                chunks = _chunk_text(text)
                # Relative path within the source dir for traceability
                source_dir_path = _DEFAULT_KB_DIR if label == "knowledge_base/default" else _DOCS_DIR
                rel_path = fpath.relative_to(source_dir_path)
                for i, chunk in enumerate(chunks):
                    if not chunk.strip():
                        continue
                    # Pre-validate embedding to surface Ollama failures early.
                    embedding = await embedder.async_get_embedding(chunk)
                    if not embedding:
                        log.warning(
                            "load_default_knowledge_task: empty embedding org=%s file=%s chunk=%d — skipping",
                            org_id, fpath.name, i,
                        )
                        continue
                    await knowledge.ainsert(
                        text_content=chunk,
                        name=f"{label}/{rel_path} [chunk {i + 1}/{len(chunks)}]",
                        metadata={
                            "source": GSageKnowledgeSource.SYSTEM.value,
                            "original_filename": f"{label}/{rel_path}",
                            "chunk_index": i,
                        },
                        upsert=True,
                    )
                    stored += 1

            return stored
        finally:
            evict_knowledge_cache(org_uuid)

    try:
        stored = asyncio.run(_run())
        log.info(
            "load_default_knowledge_task: org=%s stored %d chunks from %d files",
            org_id,
            stored,
            len(files),
        )
        return {"status": "completed", "chunks_stored": stored, "files": len(files)}
    except Exception as exc:
        log.error(
            "load_default_knowledge_task failed: org=%s error=%s", org_id, exc, exc_info=True
        )
        return {"status": "failed", "error": str(exc)}
