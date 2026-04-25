"""gSage AI — Wiki.js page editor tool.

Allows the LLM agent to browse, read, search, create and edit Markdown pages
in a Wiki.js instance via its GraphQL API.

Write operations (edit_page, create_page) are restricted to a configurable
path prefix (``writable_path`` in config, e.g. ``"gsage/"``).  Pages
outside that prefix are read-only.

Actions
-------
list_pages    — List pages or browse a folder tree (requires wiki:read)
search_pages  — Full-text search across all pages (requires wiki:read)
read_page     — Read full or partial (line range) page content (requires wiki:read)
grep_page     — Search for a regex pattern within a page (requires wiki:read)
edit_page     — Replace a line range in a page (requires wiki:write)
create_page   — Create a new page at a given path (requires wiki:write)
delete_page   — Permanently delete a page (requires wiki:write, path-guarded)

Permissions: ``wiki:read`` (read actions), ``wiki:write`` (write actions)
"""

from __future__ import annotations

import logging
import re
import time
import traceback
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.core.wikijs._client import WikijsClient, WikijsError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_MAX_GREP_MATCHES = 50
_DEFAULT_LIST_LIMIT = 50


class WikijsEditorTool(BaseTool):
    """Browse, read, search, create and edit Wiki.js Markdown pages.

    Use this tool to interact with the team's Wiki.js documentation instance.
    All read operations (list, read, grep) require the ``wiki:read`` permission.
    Write operations (edit, create) require ``wiki:write`` and are restricted to
    the configured ``writable_path`` prefix.

    **Actions:**

    ``list_pages``
      List all pages or, if ``path`` is provided, show the page tree under
      that folder prefix. Returns id, path, title, and updatedAt for each item.

    ``read_page``
      Read a page's Markdown content. Use ``page_id`` or ``path`` to identify
      the page. Use ``line_start`` and ``line_end`` (1-based, inclusive) to
      fetch only a portion of the content — useful for large pages.

    ``grep_page``
      Search for a regex ``pattern`` within a specific page's content.
      Returns matching lines with their line numbers (max 50 matches).

    ``edit_page``
      Replace a range of lines (``line_start``–``line_end``, 1-based inclusive)
      in a page with ``new_content``. Requires ``wiki:write`` and the page must
      be inside the configured ``writable_path``.

    ``search_pages``
      Full-text search across all Wiki.js pages. Returns matching pages with
      id, path, title, and description. Use ``path`` to restrict the search
      scope to a folder prefix.

    ``create_page``
      Create a new page at the given ``path`` with the provided ``title`` and
      ``content``. Requires ``wiki:write`` and the path must be inside the
      configured ``writable_path``.

    ``delete_page``
      Permanently delete a page identified by ``page_id`` or ``path``. This
      operation is **irreversible**. Requires ``wiki:write`` and the page must
      be inside the configured ``writable_path``.

    Permissions: ``wiki:read`` (list/read/grep/search), ``wiki:write`` (edit/create/delete)
    """

    name: ClassVar[str] = "wikijs_editor"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Browse, read, search, create, edit, and delete Wiki.js Markdown pages (wiki, knowledge base, wikijs). Use for any wiki or documentation operation."
    category: ClassVar[str] = "kb"
    permissions: ClassVar[list[str]] = ["wiki:read", "wiki:write"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    supports_multiple_configs: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {"target_entities": "path"}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_pages", "search_pages", "read_page", "grep_page", "edit_page", "create_page", "delete_page"],
                "description": (
                    "Action to perform: "
                    "list_pages — list all pages or browse a folder tree; "
                    "search_pages — full-text search across all pages by keyword; "
                    "read_page — read page content (full or by line range); "
                    "grep_page — search a regex pattern inside a page; "
                    "edit_page — replace a line range in a page (write:required, path-guarded); "
                    "create_page — create a new page (write:required, path-guarded); "
                    "delete_page — permanently delete a page (write:required, path-guarded, irreversible)."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Page path or folder prefix. "
                    "For list_pages: folder prefix to browse (e.g. 'gsage'). "
                    "For read_page/grep_page: full page path (e.g. 'gsage/overview'). "
                    "For create_page: full path for the new page. "
                    "For edit_page: used to locate the page when page_id is unknown."
                ),
            },
            "page_id": {
                "type": "integer",
                "description": (
                    "Wiki.js numeric page ID. "
                    "Used by read_page, grep_page, and edit_page to identify the page. "
                    "Preferred over 'path' when known — avoids an extra lookup."
                ),
            },
            "line_start": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "First line to read or replace (1-based inclusive). "
                    "Used by read_page (partial read) and edit_page (line range replacement)."
                ),
            },
            "line_end": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Last line to read or replace (1-based inclusive). "
                    "Used by read_page (partial read) and edit_page (line range replacement). "
                    "Must be >= line_start."
                ),
            },
            "new_content": {
                "type": "string",
                "description": (
                    "Replacement text for edit_page. "
                    "Replaces the lines [line_start, line_end] (inclusive). "
                    "May contain multiple lines separated by newline characters."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Search term for search_pages. "
                    "Wiki.js performs full-text search across all page titles and content."
                ),
            },
            "pattern": {
                "type": "string",
                "description": (
                    "Python regex pattern to search within the page content for grep_page. "
                    "Case-insensitive. Returns up to 50 matching lines."
                ),
            },
            "title": {
                "type": "string",
                "description": "Page title. Required for create_page.",
            },
            "content": {
                "type": "string",
                "description": "Full Markdown content for the new page. Required for create_page.",
            },
            "description": {
                "type": "string",
                "description": "Short description/summary for create_page (optional).",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of tag strings for create_page (optional).",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "Wiki.js base URL (e.g. http://wikijs:3000). "
                    "Overrides TOOL_WIKIJS_EDITOR__URL env var."
                ),
            },
            "api_token": {
                "type": "string",
                "sensitive": True,
                "description": (
                    "Bearer token for the Wiki.js GraphQL API. "
                    "Generate in Administration > API Access. "
                    "Overrides TOOL_WIKIJS_EDITOR__API_TOKEN env var."
                ),
            },
            "writable_path": {
                "type": "string",
                "description": (
                    "Path prefix that the tool is allowed to write to "
                    "(e.g. 'gsage/'). Pages outside this prefix are read-only. "
                    "Overrides TOOL_WIKIJS_EDITOR__WRITABLE_PATH env var."
                ),
            },
            "locale": {
                "type": "string",
                "description": (
                    "Default Wiki.js locale for page lookups and creation "
                    "(e.g. 'en', 'pt'). Overrides TOOL_WIKIJS_EDITOR__LOCALE env var."
                ),
            },
        },
        "additionalProperties": False,
    }
    config_defaults: ClassVar[dict] = {
        "url": "",
        "api_token": "",
        "writable_path": "",
        "locale": "",
    }

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Helpers ────────────────────────────────────────────────────────────

    def _build_client(self, config: dict) -> WikijsClient:
        url = (config.get("url") or "").strip() or None
        token = (config.get("api_token") or "").strip() or None
        return WikijsClient(url=url, api_token=token, timeout=float(self.timeout_seconds))

    def _get_locale(self, config: dict) -> str:
        return (config.get("locale") or "").strip() or "en"

    def _get_writable_path(self, config: dict) -> str:
        return (config.get("writable_path") or "").strip()

    @staticmethod
    def _guard_writable_path(page_path: str, writable_path: str, action: str) -> Optional[str]:
        """Return error message if page_path is outside the writable_path prefix, else None."""
        if not writable_path:
            return f"writable_path is not configured — {action} is disabled."
        # Normalise: strip leading slashes for comparison
        norm_page = page_path.lstrip("/")
        norm_writable = writable_path.lstrip("/")
        if not norm_page.startswith(norm_writable):
            return (
                f"Path '{page_path}' is outside the allowed writable prefix "
                f"('{writable_path}'). Only pages under this prefix may be modified."
            )
        return None

    # ── execute ────────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()

        action: str = params["action"]

        # Determine required permission per action
        write_actions = {"edit_page", "create_page", "delete_page"}
        if action in write_actions:
            if not agent_context.has_permission("wiki:write"):
                return self._failure(
                    "PERMISSION_DENIED",
                    f"Permission 'wiki:write' is required for action '{action}'.",
                    retryable=False,
                )
        else:
            if not agent_context.has_permission("wiki:read"):
                return self._failure(
                    "PERMISSION_DENIED",
                    f"Permission 'wiki:read' is required for action '{action}'.",
                    retryable=False,
                )

        locale = self._get_locale(config)
        writable_path = self._get_writable_path(config)

        try:
            async with self._build_client(config) as client:
                if action == "list_pages":
                    result = await self._list_pages(client, params, locale)
                elif action == "search_pages":
                    result = await self._search_pages(client, params, locale)
                elif action == "read_page":
                    result = await self._read_page(client, params, locale)
                elif action == "grep_page":
                    result = await self._grep_page(client, params, locale)
                elif action == "edit_page":
                    result = await self._edit_page(client, params, locale, writable_path)
                elif action == "create_page":
                    result = await self._create_page(client, params, locale, writable_path)
                elif action == "delete_page":
                    result = await self._delete_page(client, params, locale, writable_path)
                else:
                    return self._failure("INVALID_ACTION", f"Unknown action: '{action}'", retryable=False)

        except WikijsError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            log.warning("wikijs_editor [%s] error: %s (slug=%s)", action, exc, exc.slug)
            tb_str = traceback.format_exc()
            failure = self._failure(
                exc.slug or "WIKIJS_ERROR",
                str(exc),
                retryable=exc.slug in ("CONNECTION_ERROR", "HTTP_ERROR"),
                execution_time_ms=elapsed,
            )
            failure.traceback_str = tb_str
            return failure

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(result, execution_time_ms=elapsed)

    # ── Action handlers ────────────────────────────────────────────────────

    async def _search_pages(
        self,
        client: WikijsClient,
        params: dict,
        locale: str,
    ) -> dict:
        query: Optional[str] = params.get("query")
        path: Optional[str] = params.get("path") or None

        if not query:
            raise WikijsError(
                "search_pages requires a 'query' term.",
                slug="PARAM_MISSING",
            )

        results = await client.search_pages(
            query=query,
            path_prefix=path.strip("/") if path else None,
            locale=locale,
        )

        return {
            "query": query,
            "path_prefix": path,
            "locale": locale,
            "count": len(results),
            "results": results,
        }

    async def _list_pages(
        self,
        client: WikijsClient,
        params: dict,
        locale: str,
    ) -> dict:
        path: Optional[str] = params.get("path") or None

        if path:
            items = await client.get_tree(path.strip("/"), locale)
            return {
                "path_prefix": path,
                "locale": locale,
                "count": len(items),
                "items": items,
            }
        else:
            pages = await client.list_pages(limit=_DEFAULT_LIST_LIMIT)
            return {
                "count": len(pages),
                "pages": pages,
            }

    async def _read_page(
        self,
        client: WikijsClient,
        params: dict,
        locale: str,
    ) -> dict:
        page_id: Optional[int] = params.get("page_id")
        path: Optional[str] = params.get("path")
        line_start: Optional[int] = params.get("line_start")
        line_end: Optional[int] = params.get("line_end")

        if not page_id and not path:
            raise WikijsError(
                "read_page requires either 'page_id' or 'path'.",
                slug="PARAM_MISSING",
            )

        if page_id:
            page = await client.get_page(page_id)
        else:
            page = await client.get_page_by_path(path.strip("/"), locale)  # type: ignore[union-attr]

        raw_content: str = page.get("content") or ""
        lines = raw_content.split("\n")
        total_lines = len(lines)

        if line_start or line_end:
            ls = max(1, line_start or 1)
            le = min(total_lines, line_end or total_lines)
            if ls > le:
                raise WikijsError(
                    f"line_start ({ls}) must be <= line_end ({le}).",
                    slug="PARAM_INVALID",
                )
            snippet_lines = lines[ls - 1 : le]
            # Prefix each line with its number for easy reference
            numbered = "\n".join(f"{ls + i}: {l}" for i, l in enumerate(snippet_lines))
            content_out = numbered
        else:
            ls = 1
            le = total_lines
            # For full content, number lines so the LLM can reference them
            numbered = "\n".join(f"{i + 1}: {l}" for i, l in enumerate(lines))
            content_out = numbered

        return {
            "page_id": page["id"],
            "path": page["path"],
            "title": page["title"],
            "total_lines": total_lines,
            "line_start": ls,
            "line_end": le,
            "content": content_out,
        }

    async def _grep_page(
        self,
        client: WikijsClient,
        params: dict,
        locale: str,
    ) -> dict:
        page_id: Optional[int] = params.get("page_id")
        path: Optional[str] = params.get("path")
        pattern: Optional[str] = params.get("pattern")

        if not page_id and not path:
            raise WikijsError(
                "grep_page requires either 'page_id' or 'path'.",
                slug="PARAM_MISSING",
            )
        if not pattern:
            raise WikijsError("grep_page requires a 'pattern'.", slug="PARAM_MISSING")

        if page_id:
            page = await client.get_page(page_id)
        else:
            page = await client.get_page_by_path(path.strip("/"), locale)  # type: ignore[union-attr]

        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise WikijsError(
                f"Invalid regex pattern: {exc}",
                slug="PARAM_INVALID",
            ) from exc

        raw_content: str = page.get("content") or ""
        lines = raw_content.split("\n")
        matches = []
        for i, line in enumerate(lines, start=1):
            if rx.search(line):
                matches.append({"line_number": i, "line_content": line})
            if len(matches) >= _MAX_GREP_MATCHES:
                break

        return {
            "page_id": page["id"],
            "path": page["path"],
            "pattern": pattern,
            "total_matches": len(matches),
            "truncated": len(matches) >= _MAX_GREP_MATCHES,
            "matches": matches,
        }

    async def _edit_page(
        self,
        client: WikijsClient,
        params: dict,
        locale: str,
        writable_path: str,
    ) -> dict:
        page_id: Optional[int] = params.get("page_id")
        path: Optional[str] = params.get("path")
        line_start: Optional[int] = params.get("line_start")
        line_end: Optional[int] = params.get("line_end")
        new_content: Optional[str] = params.get("new_content")

        if not page_id and not path:
            raise WikijsError(
                "edit_page requires either 'page_id' or 'path'.",
                slug="PARAM_MISSING",
            )
        if line_start is None or line_end is None:
            raise WikijsError(
                "edit_page requires 'line_start' and 'line_end'.",
                slug="PARAM_MISSING",
            )
        if new_content is None:
            raise WikijsError(
                "edit_page requires 'new_content'.",
                slug="PARAM_MISSING",
            )
        if line_start > line_end:
            raise WikijsError(
                f"line_start ({line_start}) must be <= line_end ({line_end}).",
                slug="PARAM_INVALID",
            )

        # Fetch current page
        if page_id:
            page = await client.get_page(page_id)
        else:
            page = await client.get_page_by_path(path.strip("/"), locale)  # type: ignore[union-attr]

        # Path guard
        error = self._guard_writable_path(page["path"], writable_path, "edit_page")
        if error:
            raise WikijsError(error, slug="PATH_NOT_WRITABLE")

        raw_content: str = page.get("content") or ""
        lines = raw_content.split("\n")
        total_before = len(lines)

        ls = max(1, line_start)
        le = min(total_before, line_end)

        replacement_lines = new_content.split("\n")
        new_lines = lines[: ls - 1] + replacement_lines + lines[le:]
        updated_content = "\n".join(new_lines)

        updated_page = await client.update_page(page["id"], content=updated_content)

        return {
            "page_id": page["id"],
            "path": page["path"],
            "title": page["title"],
            "lines_replaced": le - ls + 1,
            "replacement_lines": len(replacement_lines),
            "total_lines_before": total_before,
            "total_lines_after": len(new_lines),
            "updated_at": updated_page.get("updatedAt"),
        }

    async def _create_page(
        self,
        client: WikijsClient,
        params: dict,
        locale: str,
        writable_path: str,
    ) -> dict:
        path: Optional[str] = params.get("path")
        title: Optional[str] = params.get("title")
        content: Optional[str] = params.get("content")
        description: str = params.get("description") or ""
        tags: list[str] = params.get("tags") or []

        if not path:
            raise WikijsError("create_page requires 'path'.", slug="PARAM_MISSING")
        if not title:
            raise WikijsError("create_page requires 'title'.", slug="PARAM_MISSING")
        if content is None:
            raise WikijsError("create_page requires 'content'.", slug="PARAM_MISSING")

        norm_path = path.strip("/")

        # Auto-correct path: if it falls outside writable_path, prepend the prefix
        # so the operation succeeds. The caller is warned via path_adjusted in the result.
        path_adjusted = False
        original_path: Optional[str] = None
        if writable_path:
            norm_writable = writable_path.lstrip("/")
            if not norm_path.startswith(norm_writable):
                original_path = norm_path
                norm_path = norm_writable.rstrip("/") + "/" + norm_path
                path_adjusted = True
        else:
            # writable_path not configured — block write
            raise WikijsError(
                "writable_path is not configured — create_page is disabled.",
                slug="CONFIG_MISSING",
            )

        page = await client.create_page(
            path=norm_path,
            title=title,
            content=content,
            locale=locale,
            description=description,
            tags=tags,
        )

        result: dict = {
            "page_id": page.get("id"),
            "path": page.get("path"),
            "title": page.get("title"),
            "created_at": page.get("createdAt"),
        }
        if path_adjusted:
            result["warning"] = (
                f"The path '{original_path}' was outside the allowed prefix "
                f"('{writable_path}'). The page was created at '{norm_path}' instead."
            )
            result["path_adjusted"] = True
            result["original_path_requested"] = original_path
        return result

    async def _delete_page(
        self,
        client: WikijsClient,
        params: dict,
        locale: str,
        writable_path: str,
    ) -> dict:
        page_id: Optional[int] = params.get("page_id")
        path: Optional[str] = params.get("path")

        if not page_id and not path:
            raise WikijsError(
                "delete_page requires either 'page_id' or 'path'.",
                slug="PARAM_MISSING",
            )

        # Fetch page metadata to apply the writable-path guard
        if page_id:
            page = await client.get_page(page_id)
        else:
            page = await client.get_page_by_path(path.strip("/"), locale)  # type: ignore[union-attr]

        error = self._guard_writable_path(page["path"], writable_path, "delete_page")
        if error:
            raise WikijsError(error, slug="PATH_NOT_WRITABLE")

        await client.delete_page(page["id"])

        return {
            "deleted": True,
            "page_id": page["id"],
            "path": page["path"],
            "title": page["title"],
        }
