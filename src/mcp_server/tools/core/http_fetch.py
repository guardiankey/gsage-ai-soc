"""gSage AI — HTTP Fetch tool.

Generic HTTP(S) fetch with HTML→Markdown conversion and artifact storage.
The agent uses this to retrieve web content on demand.

Permission: ``core:http:fetch``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

import httpx

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.http_utils import (
    extension_for_content_type,
    fetch_url,
    html_to_markdown,
    is_text_content_type,
    url_hash,
    url_slug,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_DEFAULT_PREVIEW_CHARS = 4000
_MAX_CONTENT_LENGTH = 20_000_000
AGENT_HINT_TEXT = (
    "⚠️ Risco: o simples acesso a uma URL pode vazar dados internos "
    "(IP de origem, User-Agent), disparar efeitos colaterais no servidor "
    "de destino, ou expor o agente a conteúdo malicioso. "
    "Só acesse URLs confiáveis. Prefira fontes oficiais (.gov.br, .rnp.br) "
    "e evite encurtadores."
)

AGENT_HINT_BINARY = (
    "ℹ️ Binary file — content saved as artifact. "
    "Use read_file if the file is text-based (JSON, CSV, log), "
    "or inform the user that the download link is available via the artifact."
)


class HttpFetchTool(BaseTool):
    """Fetch a web page over HTTP(S) and return its content as Markdown.

    The tool downloads the HTML, extracts the main content, and converts it
    to Markdown via ``trafilatura``.  The full result is saved as an artifact;
    only the first *preview_chars* characters are returned inline.

    Permission: ``core:http:fetch``.
    """

    name: ClassVar[str] = "http_fetch"
    version: ClassVar[str] = "1.1.0"
    summary: ClassVar[str] = (
        "Fetch a web page or file (HTTP/HTTPS). "
        "Text content (HTML, JSON, XML, etc.) is converted to Markdown "
        "and returned as an inline preview. "
        "Binary content (PDF, ZIP, images, Office docs, etc.) is saved "
        "as an artifact without an inline preview."
    )
    category: ClassVar[str] = "utility"
    permissions: ClassVar[list[str]] = ["core:http:fetch"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = False
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = False
    requires_config: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {
                "type": "string",
                "description": "Full http/https URL to fetch.",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST"],
                "description": "HTTP method (default GET).",
            },
            "headers": {
                "type": "object",
                "description": (
                    "Extra request headers (e.g. Authorization). "
                    "User-Agent is set automatically."
                ),
            },
            "body": {
                "type": "string",
                "description": "Request body for POST.",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 60,
                "description": "Request timeout in seconds (default 30).",
            },
            "follow_redirects": {
                "type": "boolean",
                "description": "Follow HTTP redirects (default true).",
            },
            "max_content_length": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_CONTENT_LENGTH,
                "description": (
                    "Maximum bytes to download (default 5 000 000)."
                ),
            },
            "extract_markdown": {
                "type": "boolean",
                "description": (
                    "Convert HTML to Markdown via trafilatura (default true). "
                    "If false, returns raw HTML.  Ignored for binary content."
                ),
            },
            "preview_chars": {
                "type": "integer",
                "minimum": 100,
                "maximum": 50000,
                "description": (
                    "Characters to return in the inline preview "
                    f"(default {_DEFAULT_PREVIEW_CHARS}). "
                    "Ignored for binary content."
                ),
            },
            "save_artifact": {
                "type": "boolean",
                "description": (
                    "Save the full content as a downloadable artifact "
                    "(default true)."
                ),
            },
            "content_nature": {
                "type": "string",
                "enum": ["auto", "text", "binary"],
                "description": (
                    "Force interpretation mode. 'auto' (default) detects "
                    "from the Content-Type response header. "
                    "Use 'binary' when the server sends a misleading "
                    "Content-Type (e.g. a ZIP served as text/plain)."
                ),
            },
            "force_refresh": {
                "type": "boolean",
                "description": "Bypass Redis cache for this URL.",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Execute ───────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        url = (params.get("url") or "").strip()
        if not url:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_PARAMS", "'url' is required.", execution_time_ms=elapsed
            )

        method = (params.get("method") or "GET").upper()
        extract_md = params.get("extract_markdown", True)
        save_artifact = params.get("save_artifact", True)
        preview_chars = int(params.get("preview_chars") or _DEFAULT_PREVIEW_CHARS)
        content_nature_override = params.get("content_nature", "auto")

        try:
            result = await fetch_url(
                url,
                method=method,
                headers=params.get("headers"),
                body=params.get("body"),
                timeout=float(params.get("timeout_seconds") or 30),
                follow_redirects=params.get("follow_redirects", True),
                max_content_length=int(
                    params.get("max_content_length") or 5_000_000
                ),
            )
        except ValueError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_PARAMS", str(exc), execution_time_ms=elapsed
            )
        except httpx.HTTPError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "CONNECTION_ERROR",
                str(exc),
                retryable=True,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("http_fetch: unexpected error for %s", url)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        raw_body = result["body"]
        content_type = result["content_type"]

        # Determine content nature: explicit override > auto-detection
        if content_nature_override == "binary":
            is_binary = True
        elif content_nature_override == "text":
            is_binary = False
        else:
            is_binary = not is_text_content_type(content_type)

        content_nature = "binary" if is_binary else "text"

        # ── Text path (existing behaviour, unchanged) ──────────────────
        if not is_binary:
            # Convert to Markdown if requested and content is HTML
            if extract_md and content_type.startswith("text/html"):
                try:
                    text = raw_body.decode("utf-8", errors="replace")
                    content = html_to_markdown(text)
                except Exception:
                    content = raw_body.decode("utf-8", errors="replace")
            else:
                content = raw_body.decode("utf-8", errors="replace")

            preview = content[:preview_chars]
            content_truncated = len(content) > preview_chars
            artifact = await self._save_artifact(
                data=content.encode("utf-8"),
                filename=f"http_fetch_{time.strftime('%Y%m%d_%H%M%S')}_{url_slug(url)}.md",
                content_type="text/markdown",
                agent_context=agent_context,
                url=url,
                save_artifact=save_artifact,
            )

            elapsed = int((time.monotonic() - t0) * 1000)
            return self._success(
                data={
                    "url": url,
                    "final_url": result["final_url"],
                    "status_code": result["status_code"],
                    "content_type": content_type,
                    "content_nature": content_nature,
                    "content_length": result["content_length"],
                    "title": result["title"],
                    "preview": preview,
                    "content_truncated": content_truncated,
                    "preview_chars": preview_chars,
                    "artifact": artifact,
                    "agent_hint": AGENT_HINT_TEXT,
                },
                execution_time_ms=elapsed,
            )

        # ── Binary path (NEW) ──────────────────────────────────────────
        ext = extension_for_content_type(content_type)
        filename = f"http_fetch_{time.strftime('%Y%m%d_%H%M%S')}_{url_slug(url)}{ext}"

        artifact = await self._save_artifact(
            data=raw_body,
            filename=filename,
            content_type=content_type,
            agent_context=agent_context,
            url=url,
            save_artifact=save_artifact,
        )

        # Warn if content was truncated
        hint_parts = [AGENT_HINT_BINARY]
        max_len = int(params.get("max_content_length") or 5_000_000)
        if result["content_length"] >= max_len:
            hint_parts.append(
                f"⚠️ Download was truncated at {max_len:,} bytes. "
                "The artifact is incomplete."
            )
        if not save_artifact:
            hint_parts.append(
                "⚠️ save_artifact=false — no download is available."
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={
                "url": url,
                "final_url": result["final_url"],
                "status_code": result["status_code"],
                "content_type": content_type,
                "content_nature": content_nature,
                "content_length": result["content_length"],
                "title": result["title"],
                "preview": None,
                "content_truncated": False,
                "preview_chars": 0,
                "artifact": artifact,
                "agent_hint": "\n".join(hint_parts),
            },
            execution_time_ms=elapsed,
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _save_artifact(
        self,
        data: bytes,
        filename: str,
        content_type: str,
        agent_context: AgentContext,
        url: str,
        save_artifact: bool,
    ) -> Optional[dict]:
        """Store *data* as an artifact, returning the file dict or ``None``."""
        if not save_artifact:
            return None
        try:
            from sqlalchemy.ext.asyncio import AsyncSession

            from src.mcp_server.tools.base import _tool_session_ctx

            session = _tool_session_ctx.get()
            if session is None:
                return None
            return await self._store_file(
                data=data,
                filename=filename,
                content_type=content_type,
                agent_context=agent_context,
                session=session,
                description=f"HTTP fetch of {url}",
            )
        except Exception:
            log.warning(
                "http_fetch: failed to save artifact for %s", url, exc_info=True
            )
            return None
