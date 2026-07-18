"""gSage AI — Summarize File tool.

Summarizes a large Markdown (.md) or plain-text (.txt) file by chunking it
and calling an LLM incrementally.  Runs always in background.

Permission: ``files:read`` + ``files:write``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB max source file
_CHARS_PER_TOKEN = 3.8  # ~3.8 chars per token for Portuguese
_MIME_MD = "text/markdown"
_MIME_TXT = "text/plain"
_ACCEPTED_MIME = frozenset((_MIME_MD, _MIME_TXT))

# Smart cut delimiters in priority order: (regex_pattern, label)
_SMART_CUT_DELIMITERS: list[tuple[str, str]] = [
    (r"\n\n", "paragraph"),
    (r"\n", "newline"),
    (r"\.\s", "sentence_end"),
    (r"\s", "space"),
]

# ── Helpers ──────────────────────────────────────────────────────────────────


def _find_smart_cut_point(text: str, target: int, search_window: int) -> int:
    """Find a natural cut point at or before *target*.

    Searches backwards from *target* up to *search_window* characters for
    a natural delimiter (paragraph break, newline, sentence end, space).
    Returns the position just after the delimiter, or *target* if none found.
    """
    if target >= len(text):
        return len(text)

    search_start = max(0, target - search_window)

    for pattern_str, _label in _SMART_CUT_DELIMITERS:
        # Search backwards from target
        region = text[search_start:target]
        pattern = re.compile(pattern_str)
        last_match = None
        for m in pattern.finditer(region):
            last_match = m
        if last_match is not None:
            cut = search_start + last_match.end()
            return min(cut, len(text))

    return target


def _build_initial_prompt(chunk_text: str, instructions: str = "") -> str:
    """Build the initial summarization prompt for the first chunk."""
    instr_block = ""
    if instructions.strip():
        instr_block = (
            "ADDITIONAL INSTRUCTIONS FROM USER:\n"
            f"{instructions.strip()}\n\n"
        )

    return (
        "Summarize the following text excerpt in a clear, structured way.\n\n"
        "RULES:\n"
        "- Preserve names of people, organizations, and roles exactly as they "
        "appear in the text. If the text labels speakers (e.g. "
        "'Gildenora Batista Dantas 0:11'), use those names — do NOT replace "
        "them with 'Falante 1' or 'Speaker 1'.\n"
        "- Preserve numbers, dates, monetary values, percentages, and "
        "quantitative data exactly as stated.\n"
        "- Capture key decisions, arguments, conclusions, and action items.\n"
        "- Maintain the original meaning and nuance — do not oversimplify.\n"
        "- Write in well-structured paragraphs, not bullet points or markdown "
        "headings.\n"
        "- Write in the SAME LANGUAGE as the source text.\n\n"
        "CRITICAL — DO NOT INVENT OR HALLUCINATE:\n"
        "- Only summarize what is EXPLICITLY present in the text below.\n"
        "- Do NOT add names, dates, numbers, events, or topics that are not "
        "in the source text.\n"
        "- If the text describes meeting X, do NOT mix in content from "
        "meeting Y or any other context.\n"
        "- If you are unsure about something, OMIT it rather than guess.\n\n"
        f"{instr_block}"
        "---\n"
        "TEXT TO SUMMARIZE:\n"
        f"{chunk_text}\n"
        "---"
    )


def _build_continuation_prompt(
    chunk_text: str,
    summary_tail: str,
    instructions: str = "",
) -> str:
    """Build the continuation prompt for subsequent chunks."""
    instr_block = ""
    if instructions.strip():
        instr_block = (
            "ADDITIONAL INSTRUCTIONS FROM USER:\n"
            f"{instructions.strip()}\n\n"
        )

    return (
        "Continue summarizing the document. Integrate the new excerpt below "
        "into the accumulated summary.\n\n"
        "RULES:\n"
        "- NEVER remove or shorten information already summarized, unless it "
        "is an exact duplicate of something already present.\n"
        "- Preserve EVERY previous topic in full. The existing summary is "
        "complete and correct — only add to it.\n"
        "- Integrate new information from the excerpt below into the "
        "appropriate section of the existing summary. If a topic is new, "
        "append it.\n"
        "- Do NOT rewrite, condense, or abbreviate earlier sections to make "
        "room for newer ones.\n"
        "- Preserve names exactly as they appear in the text. If the text "
        "labels speakers with names, use those names.\n"
        "- Maintain the SAME LANGUAGE, style, and level of detail.\n\n"
        "CRITICAL — DO NOT INVENT OR HALLUCINATE:\n"
        "- Only integrate information EXPLICITLY present in the new excerpt.\n"
        "- Do NOT add names, dates, numbers, or events from outside the text.\n"
        "- Do NOT mix in content from other meetings, documents, or contexts.\n\n"
        f"{instr_block}"
        "---\n"
        "END OF ACCUMULATED SUMMARY (last portion for context):\n"
        f"{summary_tail}\n\n"
        "---\n"
        "NEW EXCERPT (continuing from where the summary left off):\n"
        f"{chunk_text}\n"
        "---\n\n"
        "Summarize ONLY the new excerpt above. Return ONLY the new content "
        "to be appended to the existing summary — do NOT repeat or rewrite "
        "the existing summary.\n\n"
        "IMPORTANT:\n"
        "- Output ONLY the new paragraphs to add.\n"
        "- Do NOT include any part of the existing summary.\n"
        "- The existing summary already covers everything before this excerpt.\n"
        "- Write in the same style and language."
    )


def _build_metadata_header(
    *,
    source_filename: str,
    source_file_id: str,
    source_size_chars: int,
    iterations: int,
    llm_provider: str,
    llm_model: str,
    instructions: str,
    summary_size_chars: int,
    covered: bool,
    max_iterations: int,
    covered_chars: int = 0,
) -> str:
    """Build the metadata header block (Markdown blockquote)."""
    instr_display = (
        instructions.strip()[:100] if instructions.strip() else "(padrão)"
    )
    ratio = (summary_size_chars / source_size_chars * 100) if source_size_chars else 0
    timestamp = datetime.now(timezone.utc).isoformat()

    lines = [
        "> **Resumo gerado por IA** — `summarize_file` v1.2.0",
        f"> **Documento original:** `{source_filename}` (`{source_file_id}`)",
        f"> **Data da sumarização:** `{timestamp}`",
        f"> **Tamanho original:** `{source_size_chars}` caracteres",
        f"> **Iterações:** `{iterations}` chamadas LLM "
        f"| **Provider:** `{llm_provider}` / `{llm_model}`",
        f"> **Instruções:** `{instr_display}`",
        f"> **Compressão:** `{ratio:.1f}%` "
        f"(`{source_size_chars}` → `{summary_size_chars}` caracteres)",
    ]

    if not covered:
        lines.append(
            f"> ⚠️ **Resumo parcial** — o arquivo excedeu o limite de "
            f"`{max_iterations}` iterações. `{covered_chars}` de "
            f"`{source_size_chars}` caracteres processados."
        )

    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _build_model_for_summarize(
    config: dict,
    agent_context: AgentContext,
):
    """Build an Agno model instance for summarization calls.

    Follows the same pattern as ``_build_model()`` in ``agent_factory.py``,
    but without a ``GSageOrganization`` dependency.  Uses tool config +
    global settings for provider/model resolution.

    Returns a tuple of ``(model, provider_name)``.
    """
    from src.shared.config.settings import get_settings  # noqa: PLC0415

    settings = get_settings()

    provider = (config.get("llm_provider") or "inherit").strip().lower()
    model_override = (config.get("llm_model") or "").strip()

    # If inherit, use the global settings provider
    if provider == "inherit":
        provider = settings.llm_provider.lower()

    if provider == "openai":
        from agno.models.openai import OpenAIChat  # noqa: PLC0415

        model_id = model_override or settings.openai_maker_model
        kwargs: dict = {"id": model_id}
        if settings.openai_api_key:
            kwargs["api_key"] = settings.openai_api_key
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        return OpenAIChat(**kwargs), provider

    if provider == "deepseek":
        from agno.models.deepseek import DeepSeek  # noqa: PLC0415

        kwargs: dict = {
            "id": model_override or settings.deepseek_maker_model,
            "base_url": settings.deepseek_base_url,
        }
        if settings.deepseek_api_key:
            kwargs["api_key"] = settings.deepseek_api_key
        return DeepSeek(**kwargs), provider

    if provider == "gemini":
        from agno.models.google import Gemini  # noqa: PLC0415

        kwargs: dict = {"id": model_override or settings.gemini_maker_model}
        if settings.gemini_api_key:
            kwargs["api_key"] = settings.gemini_api_key
        return Gemini(**kwargs), provider

    if provider == "anthropic":
        from agno.models.anthropic import Claude  # noqa: PLC0415

        kwargs: dict = {"id": model_override or settings.anthropic_maker_model}
        if settings.anthropic_api_key:
            kwargs["api_key"] = settings.anthropic_api_key
        return Claude(**kwargs), provider

    if provider == "vllm":
        model_id = model_override or settings.vllm_maker_model
        from src.shared.llm.vllm_recovering import (  # noqa: PLC0415
            RecoveringToolCallVLLM,
        )

        return RecoveringToolCallVLLM(
            id=model_id,
            api_key=settings.vllm_api_key or "EMPTY",
            base_url=settings.vllm_base_url,
            timeout=300.0,
        ), provider

    # Default: Ollama
    from agno.models.ollama import Ollama  # noqa: PLC0415

    return Ollama(
        id=model_override or settings.ollama_maker_model,
        host=settings.ollama_base_url,
    ), provider


async def _call_llm(
    model,
    provider: str,
    prompt: str,
    temperature: float = 0.3,
    timeout: float = 120.0,
) -> str:
    """Call the LLM with a single user message, returning the text response.

    Uses the raw async_client (OpenAI-compatible API) for OpenAI, DeepSeek,
    vLLM, and Ollama.  Uses Agno's ainvoke for Anthropic and Gemini (which
    have non-OpenAI-compatible APIs).
    """
    # OpenAI-compatible providers: use raw chat.completions API.
    # This avoids the Agno Agent's system prompt leaking into the call.
    if provider in ("openai", "deepseek", "vllm", "ollama"):
        # Use get_async_client() — the async_client property may be None
        # until lazy-initialized via the getter.
        client = model.get_async_client()
        model_id = getattr(model, "id", "unknown")
        if client is None:
            raise RuntimeError(
                f"Model for provider '{provider}' has no async_client."
            )
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            ),
            timeout=timeout,
        )
        return response.choices[0].message.content or ""

    # Anthropic / Gemini: fall back to Agno ainvoke
    from agno.models.message import Message  # noqa: PLC0415

    response = await asyncio.wait_for(
        model.ainvoke(
            messages=[Message(role="user", content=prompt)],
            assistant_message=Message(role="assistant", content=""),
        ),
        timeout=timeout,
    )
    return response.content or ""


# ── Tool ─────────────────────────────────────────────────────────────────────


class SummarizeFileTool(BaseTool):
    """Summarize a large Markdown (.md) or plain-text (.txt) file.

    Chunks the file, calls an LLM incrementally to build a structured
    summary, and saves the result as a downloadable artifact.  Runs always
    in background.
    """

    name: ClassVar[str] = "summarize_file"
    version: ClassVar[str] = "1.2.0"
    summary: ClassVar[str] = (
        "Summarize a large Markdown or plain-text file using an LLM "
        "incrementally. Runs in background; result is injected into the "
        "conversation when complete."
    )
    category: ClassVar[str] = "file"
    permissions: ClassVar[list[str]] = ["files:read", "files:write"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 900  # 15 min
    requires_approval: ClassVar[bool] = False
    use_circuit_breaker: ClassVar[bool] = False
    always_background: ClassVar[bool] = True
    background_timeout_seconds: ClassVar[Optional[int]] = 52000  # 20 h
    requires_config: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["file_id"],
        "properties": {
            "file_id": {
                "type": "string",
                "description": (
                    "UUID of the file to summarize. Only Markdown (.md) and "
                    "plain text (.txt). Other formats must be converted with "
                    "convert_to_md first."
                ),
            },
            "output_filename": {
                "type": "string",
                "description": (
                    "Optional output filename. "
                    "Defaults to 'resumo_{original_name}.md'."
                ),
            },
            "instructions": {
                "type": "string",
                "description": (
                    "Custom summarization instructions injected into the LLM "
                    "prompt. If omitted, a default prompt is used."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "llm_provider": {
                "type": "string",
                "enum": [
                    "inherit", "ollama", "vllm", "openai", "deepseek",
                    "anthropic", "gemini",
                ],
                "default": "inherit",
                "description": (
                    "LLM provider for summarization calls. "
                    "'inherit' uses the same provider/model as the agent."
                ),
            },
            "llm_model": {
                "type": "string",
                "description": (
                    "Model name override. If empty, uses the provider's "
                    "default maker model from settings."
                ),
            },
            "llm_temperature": {
                "type": "number",
                "minimum": 0,
                "maximum": 2,
                "default": 0.1,
                "description": (
                    "Temperature for summarization calls. Lower = more "
                    "deterministic, less hallucination. 0.0-0.2 "
                    "recommended for factual tasks."
                ),
            },
            "chunk_token_target": {
                "type": "integer",
                "minimum": 1000,
                "maximum": 64000,
                "default": 12000,
                "description": (
                    "Target tokens per chunk (~3.8 chars/token). "
                    "Auto-reduced if LLM reports context limit exceeded."
                ),
            },
            "chunk_min_chars": {
                "type": "integer",
                "minimum": 1000,
                "maximum": 16000,
                "default": 4000,
                "description": (
                    "Absolute minimum chunk size. Prevents infinite "
                    "reduction loops."
                ),
            },
            "chunk_overlap_chars": {
                "type": "integer",
                "minimum": 0,
                "maximum": 8000,
                "default": 500,
                "description": (
                    "Characters from the END of the previous chunk to "
                    "include at the START of the next chunk."
                ),
            },
            "summary_tail_tokens": {
                "type": "integer",
                "minimum": 0,
                "maximum": 4000,
                "default": 2000,
                "description": (
                    "Approximate tokens from the END of the accumulated "
                    "summary to show as context (~3.8 chars/token). "
                    "Higher values preserve more coherence across "
                    "iterations but consume more prompt space."
                ),
            },
            "max_iterations": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "default": 500,
                "description": (
                    "Maximum LLM iterations before stopping. Returns "
                    "partial summary with warning if reached."
                ),
            },
            "checkpoint_interval": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "default": 5,
                "description": (
                    "Save checkpoint every N chunks. 0 = no checkpointing."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_defaults: ClassVar[dict] = {
        "llm_provider": "inherit",
        "llm_model": "",
        "llm_temperature": 0.1,
        "llm_timeout": 300,
        "chunk_token_target": 12000,
        "chunk_min_chars": 4000,
        "chunk_overlap_chars": 500,
        "summary_tail_tokens": 2000,
        "max_iterations": 500,
        "checkpoint_interval": 5,
    }

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Helpers ───────────────────────────────────────────────────────────

    def _extract_token_limit_error(self, exc: Exception) -> bool:
        """Return True if *exc* indicates a token/context limit error."""
        msg = str(exc).lower()
        indicators = [
            "token", "context", "maximum context", "max token",
            "reduce the length", "too long", "input length",
            "context length", "400",  # HTTP 400 often = context too long
        ]
        return any(ind in msg for ind in indicators)

    async def _save_checkpoint(
        self,
        accumulated: str,
        pos: int,
        iteration: int,
        chunk_size: int,
    ) -> None:
        """Save checkpoint state for resume-after-crash."""
        # Checkpoint is stored in memory for the current execution.
        # If the Celery worker dies, the task is re-enqueued (acks_late=True)
        # and the next run loads from the persisted GSageBackgroundTask row.
        # We store minimal state in instance variables for the
        # _dispatch_background mechanism to serialize.
        self._checkpoint = {
            "accumulated": accumulated,
            "position": pos,
            "iteration": iteration,
            "chunk_size": chunk_size,
        }

    # ── Execute ───────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()

        # ── Calculate deadline for graceful timeout ────────────────────
        # Reserve 120 s for the final LLM call + cleanup (store artifact,
        # build output).  The background worker's asyncio.wait_for will
        # hard-cancel us once bg_timeout is reached; the deadline check
        # below gives us a chance to save a checkpoint *before* that happens.
        _bg_timeout = self.background_timeout_seconds or 900
        deadline = t0 + _bg_timeout - 120

        # ── Validate file_id ───────────────────────────────────────────
        file_id = params.get("file_id", "")
        if not isinstance(file_id, str) or not file_id.strip():
            return self._failure("INVALID_PARAMS", "'file_id' is required.")

        # ── Load file ──────────────────────────────────────────────────
        file_meta = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id),
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            max_bytes=_MAX_FILE_BYTES,
        )
        if file_meta is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File '{file_id}' not found or access denied.",
            )

        filename: str = file_meta.get("filename", "unknown")
        content_type: str = file_meta.get("content_type", "")
        data: bytes = file_meta.get("data", b"")

        if not data:
            return self._failure("EMPTY_FILE", f"File '{filename}' is empty.")

        if content_type not in _ACCEPTED_MIME:
            return self._failure(
                "NOT_TEXT_FILE",
                f"File '{filename}' has unsupported content type "
                f"'{content_type}'. Only Markdown (.md) and plain text "
                f"(.txt) are supported. Use convert_to_md first for other "
                f"formats.",
            )

        content = data.decode("utf-8", errors="replace")
        total_chars = len(content)

        # ── Resolve config ─────────────────────────────────────────────
        chunk_chars = int(config.get("chunk_token_target", 12000) * _CHARS_PER_TOKEN)
        chunk_min = int(config.get("chunk_min_chars", 4000))
        overlap = int(config.get("chunk_overlap_chars", 500))
        tail_chars = int(
            config.get("summary_tail_tokens", 500) * _CHARS_PER_TOKEN
        )
        max_iter = int(config.get("max_iterations", 500))
        checkpoint_every = int(config.get("checkpoint_interval", 5))
        llm_timeout = int(config.get("llm_timeout", 300))
        instructions = params.get("instructions", "")

        # ── Build model ────────────────────────────────────────────────
        try:
            model, resolved_provider = _build_model_for_summarize(
                config, agent_context
            )
        except Exception as exc:
            return self._failure(
                "CONFIG_MISSING",
                f"Failed to build LLM model: {exc}",
            )

        # Resolve model name for metadata
        resolved_model = (
            config.get("llm_model", "").strip()
            or getattr(model, "id", "unknown")
        )
        temperature = config.get("llm_temperature", 0.1)
        if hasattr(model, "temperature"):
            try:
                model.temperature = temperature  # type: ignore[assignment]
            except Exception:
                pass

        # ── Check for checkpoint resume ────────────────────────────────
        checkpoint = getattr(self, "_checkpoint", None) or state.get("_checkpoint")
        if checkpoint and checkpoint.get("position", 0) > 0:
            accumulated = checkpoint["accumulated"]
            pos = checkpoint["position"]
            iteration = checkpoint["iteration"]
            chunk_chars = checkpoint["chunk_size"]
            checkpoint_was_resumed = True
            log.info(
                "summarize_file: resuming from checkpoint — "
                "pos=%d iteration=%d chunk=%d",
                pos, iteration, chunk_chars,
            )
        else:
            accumulated = ""
            pos = 0
            iteration = 0
            checkpoint_was_resumed = False

        auto_reductions = 0
        search_window = int(chunk_chars * 0.1)  # 10% for smart cut

        # ── Main loop ──────────────────────────────────────────────────
        try:
            while pos < total_chars and iteration < max_iter:
                # Smart cut: find natural delimiter.
                # For the LAST chunk (target_end == total_chars), skip smart cut
                # and take everything to the end — we must not leave content behind.
                target_end = min(pos + chunk_chars, total_chars)
                if target_end >= total_chars:
                    smart_end = total_chars  # last chunk: grab all remaining
                else:
                    smart_end = _find_smart_cut_point(
                        content, target_end, search_window
                    )

                # Overlap from previous chunk
                start = max(0, pos - overlap) if iteration > 0 else 0
                chunk_text = content[start:smart_end]

                # Summary tail for context
                summary_tail = ""
                if accumulated and tail_chars > 0:
                    summary_tail = accumulated[-tail_chars:]

                # Build prompt
                if iteration == 0:
                    prompt = _build_initial_prompt(chunk_text, instructions)
                else:
                    prompt = _build_continuation_prompt(
                        chunk_text, summary_tail, instructions,
                    )

                # LLM call with retry + auto-reduction
                response_text = None
                token_limit_hit = False

                for retry in range(3):
                    try:
                        # ── Deadline check ──────────────────────────────
                        # Stop gracefully before the background timeout
                        # fires, saving a checkpoint so the next run can
                        # resume.
                        if time.monotonic() > deadline:
                            await self._save_checkpoint(
                                accumulated, pos, iteration, chunk_chars
                            )
                            state["_checkpoint"] = {
                                "accumulated": accumulated,
                                "position": pos,
                                "iteration": iteration,
                                "chunk_size": chunk_chars,
                            }
                            pct = (pos / total_chars * 100) if total_chars else 0
                            return self._failure(
                                "TIMEOUT_PARTIAL",
                                f"Background timeout approaching — "
                                f"{pos}/{total_chars} chars "
                                f"({pct:.1f}%) processed in "
                                f"{iteration} iterations. "
                                f"Progress saved. Retry to resume.",
                                retryable=True,
                            )

                        response_text = await _call_llm(
                            model=model,
                            provider=resolved_provider,
                            prompt=prompt,
                            temperature=config.get("llm_temperature", 0.1),
                            timeout=llm_timeout,
                        )
                        break  # success
                    except asyncio.TimeoutError:
                        if retry < 2:
                            backoff = 2 ** retry
                            log.warning(
                                "summarize_file: timeout (attempt %d/3), "
                                "retrying in %ds",
                                retry + 1, backoff,
                            )
                            await asyncio.sleep(backoff)
                        else:
                            return self._failure(
                                "LLM_ERROR",
                                "LLM call timed out after 3 attempts.",
                            )
                    except Exception as exc:
                        if self._extract_token_limit_error(exc):
                            token_limit_hit = True
                            break  # break retry loop to auto-reduce
                        if retry < 2:
                            backoff = 2 ** retry
                            log.warning(
                                "summarize_file: LLM error "
                                "(attempt %d/3): %s",
                                retry + 1, exc,
                            )
                            await asyncio.sleep(backoff)
                        else:
                            return self._failure(
                                "LLM_ERROR",
                                f"LLM call failed after 3 attempts: {exc}",
                            )

                # Handle token limit: reduce chunk and retry same position
                if token_limit_hit:
                    if chunk_chars > chunk_min:
                        chunk_chars = max(int(chunk_chars * 0.75), chunk_min)
                        auto_reductions += 1
                        search_window = int(chunk_chars * 0.1)
                        log.info(
                            "summarize_file: token limit — reduced chunk "
                            "to %d chars (reduction #%d)",
                            chunk_chars, auto_reductions,
                        )
                        continue  # retry same position with smaller chunk
                    else:
                        return self._failure(
                            "LLM_ERROR",
                            f"Chunk size at minimum ({chunk_min} chars) "
                            f"but LLM still reports token limit.",
                        )

                # Success — update state
                if response_text is None or not response_text.strip():
                    return self._failure(
                        "LLM_ERROR",
                        "LLM returned empty response.",
                    )
                # First chunk: set accumulated. Subsequent: append-only.
                if iteration == 0:
                    accumulated = response_text
                else:
                    accumulated = accumulated + "\n\n" + response_text
                pos = smart_end
                iteration += 1

                log.debug(
                    "summarize_file: chunk %d done — pos=%d/%d (+%d chars, "
                    "smart_end=%d, total=%d)",
                    iteration, pos, total_chars,
                    len(response_text), smart_end, total_chars,
                )

                # Checkpoint
                if checkpoint_every > 0 and iteration % checkpoint_every == 0:
                    await self._save_checkpoint(
                        accumulated, pos, iteration, chunk_chars
                    )
                    log.debug(
                        "summarize_file: checkpoint saved at iteration %d",
                        iteration,
                    )

        except asyncio.CancelledError:
            # Hard timeout from the background worker — save checkpoint
            # so the next run can resume from this point.
            await self._save_checkpoint(
                accumulated, pos, iteration, chunk_chars
            )
            state["_checkpoint"] = {
                "accumulated": accumulated,
                "position": pos,
                "iteration": iteration,
                "chunk_size": chunk_chars,
            }
            pct = (pos / total_chars * 100) if total_chars else 0
            return self._failure(
                "TIMEOUT",
                f"Background timeout reached — {pos}/{total_chars} chars "
                f"({pct:.1f}%) processed in {iteration} iterations. "
                f"Progress saved. Retry the tool to resume from this point.",
                retryable=True,
            )

        # ── Build output ───────────────────────────────────────────────
        summary_chars = len(accumulated)
        output_filename = params.get("output_filename", "").strip()
        if not output_filename:
            safe_name = "".join(
                c if c.isalnum() or c in "._- " else "_" for c in filename
            )[:80]
            if not safe_name.lower().endswith(".md"):
                safe_name += ".md"
            output_filename = f"resumo_{safe_name}"

        header = _build_metadata_header(
            source_filename=filename,
            source_file_id=file_id,
            source_size_chars=total_chars,
            iterations=iteration,
            llm_provider=resolved_provider,
            llm_model=resolved_model,
            instructions=instructions,
            summary_size_chars=summary_chars,
            covered=(pos >= total_chars),
            max_iterations=max_iter,
            covered_chars=pos,
        )
        final_content = header + accumulated

        from src.shared.database import _get_session_maker  # noqa: PLC0415

        artifact = None
        try:
            async with _get_session_maker()() as store_session:
                artifact = await self._store_file(
                    data=final_content.encode("utf-8"),
                    filename=output_filename,
                    content_type="text/markdown",
                    agent_context=agent_context,
                    session=store_session,
                    description=f"LLM summary of {filename}",
                )
        except Exception as exc:
            log.exception(
                "summarize_file: failed to store result: %s", exc
            )

        if artifact is None:
            return self._failure(
                "STORE_FAILED",
                "Summary was generated but could not be saved. Try again later.",
                retryable=True,
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={
                "source_file_id": file_id,
                "source_filename": filename,
                "source_size_chars": total_chars,
                "summary_file": artifact,
                "stats": {
                    "iterations": iteration,
                    "llm_calls": iteration,
                    "llm_auto_reductions": auto_reductions,
                    "chunk_size_final_chars": chunk_chars,
                    "llm_provider": resolved_provider,
                    "llm_model": resolved_model,
                    "covered": pos >= total_chars,
                    "covered_note": (
                        "'covered' indicates all characters were processed. "
                        "It does NOT guarantee all topics were captured — "
                        "quality depends on chunk size, summary tail size, "
                        "and the LLM's ability to preserve content across "
                        "iterations. Always review the summary."
                    ),
                    "compression_ratio": (
                        summary_chars / total_chars if total_chars else 0
                    ),
                    "checkpoint_resumed": checkpoint_was_resumed,
                    "elapsed_seconds": elapsed / 1000.0,
                },
            },
            execution_time_ms=elapsed,
        )
