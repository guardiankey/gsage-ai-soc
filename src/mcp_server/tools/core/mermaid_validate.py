"""gSage AI — Mermaid diagram validator and renderer.

Validates a Mermaid diagram source using the official ``@mermaid-js/mermaid-cli``
(``mmdc``) binary shipped in the MCP server container. Optionally renders the
diagram as a PNG and uploads it to MinIO so the file can be downloaded, sent
by email, or attached to the conversation.

Design
------
* Validation is CHEAP (syntax + layout). The LLM is expected to call this
  tool before presenting any Mermaid diagram to the user.
* Rendering (``return_image=True``) is opt-in. On the web interface the
  client already renders ``mermaid`` code blocks natively, so asking for the
  PNG only makes sense when the user wants to download it, attach it to an
  email, or operate in a channel that cannot render SVG (CLI, Telegram).
* Execution uses ``asyncio.create_subprocess_exec`` — never blocks the event
  loop. The Puppeteer config file is written once under ``/tmp`` and reused.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import ClassVar, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.config.settings import get_settings
from src.shared.database import _get_session_maker
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared Puppeteer config — written once, reused by every invocation.
# --no-sandbox is required because the MCP server runs as a non-root user
# inside a container without user namespace support.
# ---------------------------------------------------------------------------
_PUPPETEER_CONFIG_PATH = Path(tempfile.gettempdir()) / "gsage_puppeteer.json"
_PUPPETEER_CONFIG_READY = False


def _ensure_puppeteer_config() -> str:
    """Write the Puppeteer config file on first call and return its path."""
    global _PUPPETEER_CONFIG_READY
    if not _PUPPETEER_CONFIG_READY:
        _PUPPETEER_CONFIG_PATH.write_text(
            json.dumps({"args": ["--no-sandbox", "--disable-setuid-sandbox"]}),
            encoding="utf-8",
        )
        _PUPPETEER_CONFIG_READY = True
    return str(_PUPPETEER_CONFIG_PATH)


class MermaidValidateTool(BaseTool):
    """
    Validate — and optionally render — a Mermaid diagram.

    Usage policy (enforced via system prompt)
    -----------------------------------------
    Before presenting ANY Mermaid diagram to the user you MUST call this
    tool with ``diagram_text`` set to the proposed source. If validation
    fails, fix the diagram based on ``error_message`` and validate again.
    Do not show unvalidated diagrams to the user.

    Parameters
    ----------
    diagram_text:
        The Mermaid diagram source — WITHOUT surrounding ``` fences.
    return_image:
        When ``True`` (default: ``False``) the diagram is rendered as a
        PNG, uploaded to MinIO, and a short-lived download link is
        returned in ``data.file``. Use this ONLY when the user asks for
        a downloadable image or when the target channel cannot render
        Mermaid natively (email, Telegram, etc.). On the web client the
        diagram code block is already rendered interactively — no PNG
        needed.

    Returns (data)
    --------------
    - ``is_valid``: bool
    - ``error_message``: str | None  — stderr from mmdc when invalid
    - ``file``: dict | None          — present when ``return_image=True``
      and validation succeeded; contains ``file_id``, ``filename``,
      ``size_bytes``, ``download_path``, ``expires_at``.
    """

    name: ClassVar[str] = "mermaid_validate"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Validate Mermaid diagram syntax (and optionally render a PNG). "
        "MUST be called before showing any Mermaid diagram to the user."
    )
    category: ClassVar[str] = "utility"
    core_tool: ClassVar[bool] = True

    permissions: ClassVar[list[str]] = []
    use_circuit_breaker: ClassVar[bool] = False
    rate_limit_per_minute: ClassVar[int] = 30
    # mmdc spins up headless Chromium — allow generous timeout for cold starts.
    timeout_seconds: ClassVar[int] = 60

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "diagram_text": {
                "type": "string",
                "description": (
                    "Mermaid diagram source WITHOUT surrounding ``` fences. "
                    "Example: 'flowchart TD\\n  A --> B'."
                ),
                "minLength": 1,
            },
            "return_image": {
                "type": "boolean",
                "description": (
                    "When true, render the diagram as a PNG and store it in MinIO. "
                    "Use only when the user asks for a downloadable image or the "
                    "channel cannot render Mermaid natively (email, Telegram, CLI). "
                    "On the web client the Mermaid block is rendered interactively "
                    "so a PNG is usually unnecessary."
                ),
                "default": False,
            },
        },
        "required": ["diagram_text"],
        "additionalProperties": False,
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: object | None,
        state: object | None,
    ) -> ToolResult:
        t0 = time.monotonic()

        diagram_text: str = (params.get("diagram_text") or "").strip()
        return_image: bool = bool(params.get("return_image", False))

        if not diagram_text:
            return self._failure(
                code="INVALID_INPUT",
                message="diagram_text is required and must be non-empty.",
                retryable=False,
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        settings = get_settings()
        mmdc_bin = settings.mermaid_cli_bin or "mmdc"

        # ── Validate / render via mmdc ────────────────────────────────────
        try:
            stdout, stderr, returncode, png_bytes = await _run_mmdc(
                diagram_text=diagram_text,
                mmdc_bin=mmdc_bin,
                puppeteer_config_path=_ensure_puppeteer_config(),
                want_png=True,  # always produce the PNG — cheap and enables return_image
                timeout=settings.mermaid_validate_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return self._failure(
                code="VALIDATION_TIMEOUT",
                message=(
                    f"Mermaid validation timed out after "
                    f"{settings.mermaid_validate_timeout_seconds}s. "
                    "Consider simplifying the diagram and retrying."
                ),
                retryable=True,
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        except FileNotFoundError:
            return self._failure(
                code="MMDC_NOT_INSTALLED",
                message=(
                    f"Mermaid CLI ('{mmdc_bin}') is not installed or not on PATH. "
                    "Rebuild the mcp_server image so @mermaid-js/mermaid-cli is available."
                ),
                retryable=False,
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as exc:
            log.exception("mermaid_validate: unexpected failure")
            return self._failure(
                code="VALIDATION_ERROR",
                message=f"Unexpected validation error: {exc}",
                retryable=True,
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        if returncode != 0:
            err = (stderr or stdout or "").strip()
            # mmdc output is very verbose — trim to keep the LLM context small.
            if len(err) > 2000:
                err = err[:2000] + "\n[... truncated]"
            log.info(
                "mermaid_validate: invalid diagram (rc=%s) — %s",
                returncode, err.splitlines()[0] if err else "<no output>",
            )
            return self._success(
                data={
                    "is_valid": False,
                    "error_message": (
                        "Mermaid diagram is invalid. Fix the syntax error and "
                        f"validate again. Details: {err}"
                    ),
                    "file": None,
                },
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # ── Diagram is valid ──────────────────────────────────────────────
        file_info: Optional[dict] = None
        if return_image and png_bytes:
            try:
                async with _get_session_maker()() as db_session:
                    file_info = await self._store_file(
                        data=png_bytes,
                        filename=f"mermaid-{int(time.time())}.png",
                        content_type="image/png",
                        agent_context=agent_context,
                        session=db_session,
                        description="Rendered Mermaid diagram (PNG).",
                    )
            except Exception as exc:
                log.error("mermaid_validate: failed to store PNG: %s", exc)
                # Validation still succeeded — return is_valid=True with a warning.
                file_info = None

        return self._success(
            data={
                "is_valid": True,
                "error_message": None,
                "file": file_info,
            },
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

async def _run_mmdc(
    *,
    diagram_text: str,
    mmdc_bin: str,
    puppeteer_config_path: str,
    want_png: bool,
    timeout: int,
) -> tuple[str, str, int, Optional[bytes]]:
    """Run ``mmdc`` on ``diagram_text`` and return (stdout, stderr, rc, png).

    Writes the input to a temporary ``.mmd`` file and (optionally) reads the
    produced PNG. Temporary files are always cleaned up.
    """
    tmp_in = tempfile.NamedTemporaryFile(
        suffix=".mmd", mode="w", delete=False, encoding="utf-8"
    )
    tmp_out = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    try:
        tmp_in.write(diagram_text)
        tmp_in.close()
        tmp_out.close()

        proc = await asyncio.create_subprocess_exec(
            mmdc_bin,
            "-i", tmp_in.name,
            "-o", tmp_out.name,
            "--scale", "3",
            "--puppeteerConfigFile", puppeteer_config_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            raise

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        rc = proc.returncode or 0

        png_bytes: Optional[bytes] = None
        if want_png and rc == 0:
            try:
                with open(tmp_out.name, "rb") as f:
                    png_bytes = f.read()
            except OSError:
                png_bytes = None

        return stdout, stderr, rc, png_bytes
    finally:
        for path in (tmp_in.name, tmp_out.name):
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
