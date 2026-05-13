"""gSage AI — MinIO file store.

Wraps the MinIO Python SDK to provide a thin, async-friendly interface for
storing and retrieving tool-generated files.

All blocking MinIO calls run in a thread pool via ``asyncio.to_thread`` so
they do not block the async event loop.

Usage (from a tool's execute())::

    from src.shared.services.file_store import get_file_store

    store = get_file_store()
    record = await store.upload(
        data=csv_bytes,
        filename="report.csv",
        content_type="text/csv",
        org_id=str(agent_context.org_id),
        user_id=str(agent_context.user_id),
        tool_name=self.name,
        db=db_session,
    )
    # Downloads are proxied through the backend API — no presigned URLs.
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

log = logging.getLogger(__name__)


class FileStoreError(Exception):
    """Raised when a MinIO operation fails."""


class MinioFileStore:
    """Manages tool-generated file objects in a single MinIO bucket.

    Parameters
    ----------
    endpoint:
        MinIO host:port (e.g. "minio:9000").  HTTP by default; enable TLS
        by setting ``secure=True``.
    access_key / secret_key:
        MinIO root credentials or a dedicated IAM key.
    bucket:
        Bucket name.  Created on first use via :meth:`ensure_bucket`.
    secure:
        Use HTTPS for the MinIO connection.
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        template_bucket: str = "gsage-templates",
        kb_originals_bucket: str = "gsage-kb-originals",
    ) -> None:
        from minio import Minio  # imported here so tests can mock easily

        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self._bucket = bucket
        self._template_bucket = template_bucket
        self._kb_originals_bucket = kb_originals_bucket
        self._bucket_ensured = False
        self._template_bucket_ensured = False
        self._kb_originals_bucket_ensured = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _bucket_for(self, category: str) -> str:
        """Return the MinIO bucket name for *category*."""
        if category == "template":
            return self._template_bucket
        if category == "kb-original":
            return self._kb_originals_bucket
        return self._bucket

    def _ensure_bucket_sync(self) -> None:
        """Create the default bucket if it does not already exist (sync)."""
        if self._bucket_ensured:
            return
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
            log.info("MinIO: created bucket '%s'", self._bucket)
        self._bucket_ensured = True

    def _ensure_template_bucket_sync(self) -> None:
        """Create the template bucket if it does not already exist (sync)."""
        if self._template_bucket_ensured:
            return
        if not self._client.bucket_exists(self._template_bucket):
            self._client.make_bucket(self._template_bucket)
            log.info("MinIO: created bucket '%s'", self._template_bucket)
        self._template_bucket_ensured = True

    def _ensure_kb_originals_bucket_sync(self) -> None:
        """Create the kb-originals bucket if it does not already exist (sync)."""
        if self._kb_originals_bucket_ensured:
            return
        if not self._client.bucket_exists(self._kb_originals_bucket):
            self._client.make_bucket(self._kb_originals_bucket)
            log.info("MinIO: created bucket '%s'", self._kb_originals_bucket)
        self._kb_originals_bucket_ensured = True

    async def ensure_bucket(self) -> None:
        """Async wrapper around _ensure_bucket_sync."""
        await asyncio.to_thread(self._ensure_bucket_sync)

    async def ensure_template_bucket(self) -> None:
        """Async wrapper around _ensure_template_bucket_sync."""
        await asyncio.to_thread(self._ensure_template_bucket_sync)

    async def ensure_kb_originals_bucket(self) -> None:
        """Async wrapper around _ensure_kb_originals_bucket_sync."""
        await asyncio.to_thread(self._ensure_kb_originals_bucket_sync)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def upload(
        self,
        data: bytes,
        filename: str,
        content_type: str,
        org_id: str,
        user_id: str,
        tool_name: str,
        db,  # AsyncSession — imported lazily to avoid circular deps
        description: Optional[str] = None,
        trace_id: Optional[str] = None,
        ttl_hours: Optional[int] = None,
        category: str = "generated",
        scope: str = "user",
        session_id: Optional[str] = None,
        dept_id: Optional[str] = None,
    ) -> "GSageFile":  # type: ignore[name-defined]  # noqa: F821
        """Upload *data* to MinIO and insert a ``GSageFile`` DB row.

        Parameters
        ----------
        data:
            Raw file bytes.
        filename:
            User-facing filename (e.g. "report-2026.csv").
        content_type:
            MIME type string.
        org_id / user_id:
            UUID strings for path construction and FK.
        tool_name:
            Name of the tool that created this file.
        db:
            Open ``AsyncSession`` used to persist the DB record.
        description:
            Optional human-readable description.
        trace_id:
            Optional Agno run/trace identifier.
        ttl_hours:
            Override the global TTL.  Pass ``0`` for no expiry.
        category:
            ``"generated"`` (tool output), ``"template"`` (user uploaded), or
            ``"attachment"`` (chat message attachment).
            Templates are routed to the template bucket and never expire.
        scope:
            ``"user"`` (private), ``"organization"`` (org-wide visibility),
            or ``"department"`` (visible to members of *dept_id*).
        session_id:
            Optional UUID string of the chat session this attachment belongs to.
        dept_id:
            Optional department UUID string.  Required when ``scope="department"``.

        Returns
        -------
        GSageFile
            The persisted DB row (not yet committed — caller should commit).
        """
        from src.shared.config.settings import get_settings
        from src.shared.models.generated_file import GSageFile

        settings = get_settings()

        size = len(data)
        if size > settings.file_max_size_bytes:
            raise FileStoreError(
                f"File size {size} bytes exceeds limit of "
                f"{settings.file_max_size_bytes} bytes."
            )

        file_id = str(uuid.uuid4())
        storage_key = f"{org_id}/{user_id}/{file_id}"

        # Upload to the appropriate bucket
        bucket = self._bucket_for(category)
        if category == "template":
            await self.ensure_template_bucket()
        else:
            await self.ensure_bucket()

        def _put() -> None:
            self._client.put_object(
                bucket,
                storage_key,
                io.BytesIO(data),
                length=size,
                content_type=content_type,
            )

        await asyncio.to_thread(_put)
        log.debug("MinIO: uploaded %s (%d bytes) to bucket '%s'", storage_key, size, bucket)

        # Templates never expire; generated files use the configured TTL
        if category == "template":
            expires_at: Optional[datetime] = None
        else:
            effective_ttl = (
                ttl_hours if ttl_hours is not None else settings.file_default_ttl_hours
            )
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=effective_ttl)
                if effective_ttl > 0
                else None
            )

        # Persist DB record
        gfile = GSageFile(
            id=uuid.UUID(file_id),
            org_id=uuid.UUID(org_id),
            user_id=uuid.UUID(user_id) if user_id else None,
            tool_name=tool_name,
            trace_id=trace_id,
            filename=filename,
            content_type=content_type,
            size_bytes=size,
            storage_key=storage_key,
            description=description,
            expires_at=expires_at,
            category=category,
            scope=scope,
            session_id=uuid.UUID(session_id) if session_id else None,
            dept_id=uuid.UUID(dept_id) if dept_id else None,
        )
        db.add(gfile)
        # Caller is responsible for committing the session.
        return gfile

    async def upload_kb_original(
        self,
        data: bytes,
        filename: str,
        content_type: str,
        org_id: str,
        job_id: str,
    ) -> str:
        """Upload a knowledge base original file to the kb-originals bucket.

        Unlike :meth:`upload`, this does NOT create a ``GSageFile`` DB row.
        The storage key is stored directly on the ``GSageIngestJob``.

        Parameters
        ----------
        data:
            Raw file bytes.
        filename:
            Original filename.
        content_type:
            MIME type string.
        org_id:
            Owning organisation UUID string.
        job_id:
            The ingest job UUID string (used as path component).

        Returns
        -------
        str
            The MinIO storage key (``{org_id}/{job_id}/{filename}``).
        """
        await self.ensure_kb_originals_bucket()

        storage_key = f"{org_id}/{job_id}/{filename}"
        size = len(data)

        def _put() -> None:
            self._client.put_object(
                self._kb_originals_bucket,
                storage_key,
                io.BytesIO(data),
                length=size,
                content_type=content_type,
            )

        await asyncio.to_thread(_put)
        log.debug(
            "MinIO: uploaded kb-original %s (%d bytes) to bucket '%s'",
            storage_key, size, self._kb_originals_bucket,
        )
        return storage_key

    async def get_kb_original(self, storage_key: str):
        """Return a streaming MinIO response for a kb-original file."""
        def _get():
            return self._client.get_object(self._kb_originals_bucket, storage_key)

        return await asyncio.to_thread(_get)

    async def get_object(self, storage_key: str, category: str = "generated"):
        """Return a streaming MinIO response for *storage_key*.

        Used by the backend proxy endpoint to stream file bytes to the client
        without exposing presigned URLs or requiring MinIO to be externally
        reachable.  The caller must close the response when done.

        Returns
        -------
        urllib3.HTTPResponse
            Raw streaming response from the MinIO SDK.
        """
        bucket = self._bucket_for(category)

        def _get():
            return self._client.get_object(bucket, storage_key)

        return await asyncio.to_thread(_get)

    async def get_object_bytes(
        self,
        storage_key: str,
        category: str = "generated",
        max_bytes: int = 100 * 1024 * 1024,
    ) -> bytes:
        """Download *storage_key* from MinIO and return the raw bytes.

        Parameters
        ----------
        storage_key:
            Full MinIO object key.
        category:
            File category used to select the correct bucket.
        max_bytes:
            Hard cap on how many bytes to read (default 100 MB).  Raises
            ``FileStoreError`` if the object is larger.

        Returns
        -------
        bytes
            Complete file contents.
        """
        bucket = self._bucket_for(category)

        def _download() -> bytes:
            response = self._client.get_object(bucket, storage_key)
            try:
                chunks: list[bytes] = []
                total = 0
                for chunk in response.stream(amt=65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise FileStoreError(
                            f"Object {storage_key} exceeds read limit of {max_bytes} bytes."
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_download)

    async def replace_content(
        self,
        storage_key: str,
        data: bytes,
        content_type: str,
        category: str = "generated",
    ) -> int:
        """Overwrite an existing MinIO object with new *data* in-place.

        The DB record (``GSageFile``) is **not** updated here — callers must
        update ``size_bytes`` (and let ``onupdate`` refresh ``updated_at``).

        Parameters
        ----------
        storage_key:
            Full object key in MinIO (``{org_id}/{user_id}/{file_id}``).
        data:
            New file bytes.
        content_type:
            MIME type of the new content.
        category:
            Bucket category (default ``"generated"``).

        Returns
        -------
        int
            Size of the new content in bytes.

        Raises
        ------
        FileStoreError
            If the MinIO operation fails.
        """
        bucket = self._bucket_for(category)
        size = len(data)

        def _put() -> None:
            self._client.put_object(
                bucket,
                storage_key,
                io.BytesIO(data),
                length=size,
                content_type=content_type,
            )

        await asyncio.to_thread(_put)
        log.debug(
            "MinIO: replaced %s (%d bytes) in bucket '%s'", storage_key, size, bucket
        )
        return size

    async def delete_object(self, storage_key: str, category: str = "generated") -> None:
        """Delete *storage_key* from MinIO.

        Does NOT update the DB record — callers must set ``purged_at``.
        Silently swallows 'object not found' to make the operation idempotent.
        """
        bucket = self._bucket_for(category)

        def _delete() -> None:
            try:
                self._client.remove_object(bucket, storage_key)
                log.debug("MinIO: deleted %s from bucket '%s'", storage_key, bucket)
            except Exception as exc:
                # S3Error with code "NoSuchKey" is safe to ignore
                if "NoSuchKey" in str(exc) or "NoSuchObject" in str(exc):
                    log.debug("MinIO: %s already absent, skipping delete", storage_key)
                else:
                    raise

        await asyncio.to_thread(_delete)


@lru_cache(maxsize=1)
def get_file_store() -> MinioFileStore:
    """Return the shared :class:`MinioFileStore` singleton.

    Initialised lazily from application settings.
    """
    from src.shared.config.settings import get_settings

    settings = get_settings()
    return MinioFileStore(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.minio_bucket,
        secure=settings.minio_secure,
        template_bucket=settings.minio_template_bucket,
        kb_originals_bucket=settings.minio_kb_originals_bucket,
    )
