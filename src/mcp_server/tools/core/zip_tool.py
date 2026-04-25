"""gSage AI — zip_tool MCP tool.

Three actions in one tool:

- **zip**   — compress multiple files (by ID) into a single ZIP archive.
- **list**  — inspect the contents of a ZIP file without extracting.
- **unzip** — extract entries from a ZIP and store each in MinIO.

Permissions: ``files:read``, ``files:write``
"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import time
import zipfile
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# Max bytes read from MinIO per individual file (50 MB).
_FILE_MAX_BYTES = 50 * 1024 * 1024
_ZIP_CONTENT_TYPE = "application/zip"

# Zip-bomb guards for unzip action.
_UNZIP_MAX_TOTAL_BYTES = 100 * 1024 * 1024   # 100 MB total extracted
_UNZIP_MAX_ENTRIES = 200                       # max files to extract


class ZipTool(BaseTool):
    """
    ZIP tool — compress, inspect, or extract ZIP archives.

    Choose an action via the ``action`` parameter (default ``zip``):

    **action: zip**
    Compress multiple files into a single ZIP archive.  Provide ``file_ids``
    (list of file UUIDs).  The resulting archive is stored and a download link
    is returned.  Files that cannot be accessed are skipped and reported in
    ``skipped``; the tool fails only when **no** file could be included.

    **action: list**
    Inspect the contents of an existing ZIP file without extracting anything.
    Provide a single ``file_id`` pointing to a ZIP archive.  Returns the list
    of entries with name, sizes, and timestamps.

    **action: unzip**
    Extract entries from a ZIP archive and store each as a separate file in
    MinIO.  Provide a single ``file_id`` pointing to a ZIP archive.
    Optionally pass ``entries`` (list of entry names inside the ZIP) to
    extract only specific files; if omitted every non-directory entry is
    extracted.  Optionally pass ``output_prefix`` to prepend a string to
    every extracted filename.

    Safety guards (unzip):
    - Zip-bomb: rejects archives whose uncompressed total exceeds 100 MB or
      that contain more than 200 entries.
    - Zip-slip: rejects any entry whose path contains ``..`` or is absolute.

    Permission: ``files:read``, ``files:write``
    Timeout: 120 s · Background fallback after 60 s
    """

    name: ClassVar[str] = "zip_tool"
    version: ClassVar[str] = "2.0.0"
    summary: ClassVar[str] = "Compress, inspect, or extract ZIP archives attached to the current conversation"
    category: ClassVar[str] = "file"
    permissions: ClassVar[list[str]] = ["files:read", "files:write"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 120
    background_threshold_seconds: ClassVar[Optional[int]] = 60
    use_circuit_breaker: ClassVar[bool] = False
    always_background: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["zip", "list", "unzip"],
                "default": "zip",
                "description": (
                    "Action to perform. "
                    "'zip' — compress files into a ZIP (default). "
                    "'list' — inspect the contents of a ZIP without extracting. "
                    "'unzip' — extract files from a ZIP and store them in MinIO."
                ),
            },
            "file_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "[action=zip] List of file UUIDs to compress. "
                    "Obtain IDs from 'read_file' (list mode) or from "
                    "previous tool results."
                ),
            },
            "file_id": {
                "type": "string",
                "description": (
                    "[action=list, action=unzip] UUID of the ZIP file to inspect or extract."
                ),
            },
            "output_filename": {
                "type": "string",
                "description": (
                    "[action=zip] Base name for the resulting ZIP (without extension). "
                    "Defaults to 'files'."
                ),
            },
            "entries": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "[action=unzip] Names of specific entries inside the ZIP to extract. "
                    "If omitted, all non-directory entries are extracted."
                ),
            },
            "output_prefix": {
                "type": "string",
                "description": (
                    "[action=unzip] Optional prefix prepended to every extracted filename "
                    "(e.g. 'invoice_' → 'invoice_report.pdf')."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        action = (params.get("action") or "zip").lower()
        t0 = time.monotonic()

        if action == "zip":
            return await self._action_zip(agent_context, params, t0)
        if action == "list":
            return await self._action_list(agent_context, params, t0)
        if action == "unzip":
            return await self._action_unzip(agent_context, params, t0)

        return self._failure(
            "INVALID_ACTION",
            f"Unknown action '{action}'. Must be one of: zip, list, unzip.",
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )

    # ──────────────────────────────────────────────────────────────────────
    # action: zip
    # ──────────────────────────────────────────────────────────────────────

    async def _action_zip(
        self,
        agent_context: AgentContext,
        params: dict,
        t0: float,
    ) -> ToolResult:
        """Compress multiple files into a ZIP archive."""
        from src.shared.database import _get_session_maker  # noqa: PLC0415

        raw_ids = params.get("file_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return self._failure(
                "INVALID_INPUT",
                "'file_ids' is required for action=zip and must be a non-empty list.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        file_ids: list[str] = [str(fid).strip() for fid in raw_ids if str(fid).strip()]
        if not file_ids:
            return self._failure(
                "INVALID_INPUT",
                "'file_ids' contains no valid values.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        base_name: str = str(params.get("output_filename") or "files").strip() or "files"
        safe_name = "".join(c for c in base_name if c.isalnum() or c in "-_ .") or "files"

        included: list[dict] = []
        skipped: list[dict] = []

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            used_names: dict[str, int] = {}

            for file_id in file_ids:
                result = await self._load_file(
                    file_id=file_id,
                    org_id=str(agent_context.org_id),
                    user_id=str(agent_context.user_id),
                    dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
                    max_bytes=_FILE_MAX_BYTES,
                )
                if result is None:
                    log.warning(
                        "zip_tool[zip]: file %s not found or access denied (org=%s)",
                        file_id, agent_context.org_id,
                    )
                    skipped.append({"file_id": file_id, "reason": "not found or access denied"})
                    continue

                filename: str = result["filename"]
                entry_name = _unique_name(filename, used_names)
                zf.writestr(entry_name, result["data"])

                included.append({
                    "file_id": file_id,
                    "filename": filename,
                    "entry_name": entry_name,
                    "size_bytes": result["size_bytes"],
                })

        if not included:
            return self._failure(
                "NO_FILES_AVAILABLE",
                "None of the requested files could be found or accessed. "
                "Ensure the file IDs are correct and belong to your organisation.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        zip_bytes = zip_buf.getvalue()
        output_name = f"{safe_name}.zip"
        file_info: Optional[dict] = None
        try:
            async with _get_session_maker()() as db_session:
                file_info = await self._store_file(
                    data=zip_bytes,
                    filename=output_name,
                    content_type=_ZIP_CONTENT_TYPE,
                    agent_context=agent_context,
                    session=db_session,
                    description=(
                        f"ZIP archive containing {len(included)} file(s): "
                        + ", ".join(e["filename"] for e in included[:5])
                        + (" …" if len(included) > 5 else "")
                    ),
                )
        except Exception as exc:
            log.error("zip_tool[zip]: failed to store ZIP: %s", exc)

        if file_info is None:
            return self._failure(
                "STORE_FAILED",
                "ZIP was created but could not be saved to storage. Try again later.",
                retryable=True,
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        return self._success(
            data={
                "file": file_info,
                "included": included,
                "included_count": len(included),
                "skipped": skipped,
                "skipped_count": len(skipped),
            },
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )

    # ──────────────────────────────────────────────────────────────────────
    # action: list
    # ──────────────────────────────────────────────────────────────────────

    async def _action_list(
        self,
        agent_context: AgentContext,
        params: dict,
        t0: float,
    ) -> ToolResult:
        """List the contents of a ZIP file without extracting."""
        file_id: str | None = (params.get("file_id") or "").strip() or None
        if not file_id:
            return self._failure(
                "INVALID_INPUT",
                "'file_id' is required for action=list.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        result = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id),
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            max_bytes=_FILE_MAX_BYTES,
        )
        if result is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File '{file_id}' not found or access denied.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        if not zipfile.is_zipfile(io.BytesIO(result["data"])):
            return self._failure(
                "NOT_A_ZIP",
                f"File '{result['filename']}' is not a valid ZIP archive.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        entries: list[dict] = []
        total_uncompressed = 0

        with zipfile.ZipFile(io.BytesIO(result["data"]), "r") as zf:
            for info in zf.infolist():
                entries.append({
                    "name": info.filename,
                    "size": info.file_size,
                    "compressed_size": info.compress_size,
                    "is_dir": info.is_dir(),
                    "date_time": "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
                        *info.date_time
                    ),
                })
                total_uncompressed += info.file_size

        return self._success(
            data={
                "file_id": file_id,
                "filename": result["filename"],
                "size_bytes": result["size_bytes"],
                "total_entries": len(entries),
                "total_uncompressed_size": total_uncompressed,
                "entries": entries,
            },
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )

    # ──────────────────────────────────────────────────────────────────────
    # action: unzip
    # ──────────────────────────────────────────────────────────────────────

    async def _action_unzip(
        self,
        agent_context: AgentContext,
        params: dict,
        t0: float,
    ) -> ToolResult:
        """Extract entries from a ZIP and store each in MinIO."""
        from src.shared.database import _get_session_maker  # noqa: PLC0415

        file_id: str | None = (params.get("file_id") or "").strip() or None
        if not file_id:
            return self._failure(
                "INVALID_INPUT",
                "'file_id' is required for action=unzip.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        requested_entries: list[str] | None = params.get("entries") or None
        if isinstance(requested_entries, list):
            requested_entries = [str(e) for e in requested_entries if str(e).strip()]
            if not requested_entries:
                requested_entries = None

        output_prefix: str = str(params.get("output_prefix") or "").strip()

        result = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id),
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            max_bytes=_FILE_MAX_BYTES,
        )
        if result is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File '{file_id}' not found or access denied.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        if not zipfile.is_zipfile(io.BytesIO(result["data"])):
            return self._failure(
                "NOT_A_ZIP",
                f"File '{result['filename']}' is not a valid ZIP archive.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        extracted: list[dict] = []
        skipped: list[dict] = []

        with zipfile.ZipFile(io.BytesIO(result["data"]), "r") as zf:
            all_info = zf.infolist()

            # ── Zip-bomb guard — check totals before extracting ───────────
            non_dir = [i for i in all_info if not i.is_dir()]
            total_uncompressed = sum(i.file_size for i in non_dir)

            if len(non_dir) > _UNZIP_MAX_ENTRIES:
                return self._failure(
                    "ZIP_TOO_MANY_ENTRIES",
                    f"ZIP contains {len(non_dir)} files which exceeds the limit of "
                    f"{_UNZIP_MAX_ENTRIES}. Use 'action=list' and then pass specific "
                    f"'entries' to extract only what you need.",
                    execution_time_ms=int((time.monotonic() - t0) * 1000),
                )

            if total_uncompressed > _UNZIP_MAX_TOTAL_BYTES:
                mb = total_uncompressed // (1024 * 1024)
                return self._failure(
                    "ZIP_TOO_LARGE",
                    f"ZIP uncompressed total is {mb} MB which exceeds the "
                    f"{_UNZIP_MAX_TOTAL_BYTES // (1024 * 1024)} MB limit. "
                    f"Use 'action=list' and then pass specific 'entries' to extract "
                    f"only what you need.",
                    execution_time_ms=int((time.monotonic() - t0) * 1000),
                )

            # ── Build target set ──────────────────────────────────────────
            if requested_entries is not None:
                available_names = {i.filename for i in all_info}
                for name in requested_entries:
                    if name not in available_names:
                        skipped.append({"entry": name, "reason": "not found in ZIP"})
                target_entries = [
                    i for i in all_info
                    if not i.is_dir() and i.filename in requested_entries
                ]
            else:
                target_entries = [i for i in all_info if not i.is_dir()]

            # ── Extract and store ─────────────────────────────────────────
            async with _get_session_maker()() as db_session:
                for info in target_entries:
                    entry_name = info.filename

                    # Zip-slip guard
                    if _is_unsafe_path(entry_name):
                        log.warning(
                            "zip_tool[unzip]: rejected unsafe entry path '%s'", entry_name
                        )
                        skipped.append({
                            "entry": entry_name,
                            "reason": "unsafe path (zip-slip protection)",
                        })
                        continue

                    entry_data = zf.read(entry_name)

                    # Build output filename (basename only + optional prefix)
                    base_filename = os.path.basename(entry_name) or entry_name
                    output_name = f"{output_prefix}{base_filename}" if output_prefix else base_filename

                    content_type, _ = mimetypes.guess_type(output_name)
                    if not content_type:
                        content_type = "application/octet-stream"

                    file_meta = await self._store_file(
                        data=entry_data,
                        filename=output_name,
                        content_type=content_type,
                        agent_context=agent_context,
                        session=db_session,
                        description=f"Extracted from ZIP '{result['filename']}': {entry_name}",
                    )

                    if file_meta is None:
                        log.error(
                            "zip_tool[unzip]: failed to store entry '%s'", entry_name
                        )
                        skipped.append({
                            "entry": entry_name,
                            "reason": "storage error",
                        })
                        continue

                    extracted.append({
                        **file_meta,
                        "zip_entry": entry_name,
                    })

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if not extracted and skipped:
            return self._failure(
                "NO_FILES_EXTRACTED",
                "No entries could be extracted. See 'skipped' for details.",
                execution_time_ms=elapsed_ms,
            )

        return self._success(
            data={
                "source_file_id": file_id,
                "source_filename": result["filename"],
                "extracted": extracted,
                "extracted_count": len(extracted),
                "skipped": skipped,
                "skipped_count": len(skipped),
            },
            execution_time_ms=elapsed_ms,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _unique_name(filename: str, used: dict[str, int]) -> str:
    """Return a unique entry name for *filename* within the ZIP.

    Appends ``_N`` before the extension for duplicates:
    ``report.pdf`` → ``report_1.pdf`` → ``report_2.pdf``.

    Mutates *used* to record the new name.
    """
    if filename not in used:
        used[filename] = 1
        return filename

    if "." in filename:
        base, ext = filename.rsplit(".", 1)
        ext = f".{ext}"
    else:
        base, ext = filename, ""

    counter = used[filename]
    while True:
        candidate = f"{base}_{counter}{ext}"
        if candidate not in used:
            used[filename] = counter + 1
            used[candidate] = 1
            return candidate
        counter += 1


def _is_unsafe_path(entry_name: str) -> bool:
    """Return True when *entry_name* looks like a zip-slip attack.

    Rejects paths that:
    - Are absolute (start with ``/`` or a Windows drive letter).
    - Contain ``..`` components.
    """
    # Absolute path
    if os.path.isabs(entry_name):
        return True
    # Windows absolute  (e.g. C:\\foo)
    if len(entry_name) > 1 and entry_name[1] == ":":
        return True
    # Normalise and check for .. traversal
    normalised = os.path.normpath(entry_name)
    parts = normalised.replace("\\", "/").split("/")
    if ".." in parts:
        return True
    return False
