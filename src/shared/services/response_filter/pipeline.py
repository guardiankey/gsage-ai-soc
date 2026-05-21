"""Response filter pipeline — full-text and streaming modes."""
from __future__ import annotations

import logging
import re
from typing import AsyncIterator, Iterable, Optional

from src.shared.services.response_filter.base import (
    FilterContext,
    ResponseFilter,
)
from src.shared.services.response_filter.registry import OUTBOUND_FILTERS

log = logging.getLogger(__name__)

# A fenced code block opening, e.g. "```mermaid", "```sql", "```".
# Group 1 = language tag (may be empty). Trailing whitespace is
# horizontal-only ([ \t]*) so the regex never consumes the line's
# terminating newline.
_FENCE_OPEN_RE = re.compile(r"^```([\w+-]*)[ \t]*$", re.MULTILINE)
# Closing fence on its own line ("```" + optional horizontal whitespace).
_FENCE_CLOSE_RE = re.compile(r"^```[ \t]*$", re.MULTILINE)


def _filters_for_block(
    lang: str, filters: Iterable[ResponseFilter]
) -> list[ResponseFilter]:
    target = f"fenced_block:{lang.lower()}"
    return [f for f in filters if f.granularity == target]


def _global_filters(filters: Iterable[ResponseFilter]) -> list[ResponseFilter]:
    return [f for f in filters if f.granularity == "global"]


async def _run_block_filters(
    inner: str,
    lang: str,
    ctx: FilterContext,
    filters: Iterable[ResponseFilter],
) -> str:
    out = inner
    for f in _filters_for_block(lang, filters):
        try:
            out = await f.apply(out, ctx)
        except Exception:  # pragma: no cover — defensive
            log.exception("response_filter %r failed; passing through", f.name)
    return out


async def _run_global_filters(
    text: str,
    ctx: FilterContext,
    filters: Iterable[ResponseFilter],
) -> str:
    out = text
    for f in _global_filters(filters):
        try:
            out = await f.apply(out, ctx)
        except Exception:  # pragma: no cover — defensive
            log.exception("response_filter %r failed; passing through", f.name)
    return out


async def apply_filters_to_text(
    text: str,
    ctx: FilterContext,
    *,
    filters: Optional[Iterable[ResponseFilter]] = None,
) -> str:
    """Apply all registered filters to a complete response text.

    Used by non-streaming channels (Teams, Telegram, email, sync REST).
    """
    fs = tuple(filters) if filters is not None else OUTBOUND_FILTERS
    if not fs or not text:
        return text

    out_parts: list[str] = []
    pos = 0
    for m in _FENCE_OPEN_RE.finditer(text):
        lang = (m.group(1) or "").lower()
        close = _FENCE_CLOSE_RE.search(text, m.end())
        if not close:
            out_parts.append(text[pos:])
            pos = len(text)
            break
        out_parts.append(text[pos:m.end()])  # text + opening fence line
        inner_start = m.end()
        if inner_start < len(text) and text[inner_start] == "\n":
            inner_start += 1
            out_parts.append("\n")
        inner = text[inner_start:close.start()]
        if lang and _filters_for_block(lang, fs):
            inner = await _run_block_filters(inner, lang, ctx, fs)
        out_parts.append(inner)
        out_parts.append(text[close.start():close.end()])  # closing ```
        pos = close.end()
    out_parts.append(text[pos:])
    rebuilt = "".join(out_parts)

    return await _run_global_filters(rebuilt, ctx, fs)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class StreamFilter:
    """Incremental, streaming-aware filter state.

    Use :meth:`feed` for every incoming text chunk and emit the
    returned text. Call :meth:`flush` once at end-of-stream.

    Inside a fenced block whose language is targeted by at least one
    registered filter, content is buffered until the closing fence
    arrives; everything else is forwarded immediately. Chunks may split
    fences or labels at arbitrary boundaries — the buffer keeps a small
    tail until a newline disambiguates it.

    ``"global"`` filters cannot be applied to a streamed response without
    buffering the whole answer; they are skipped in streaming mode.
    """

    _OUTSIDE = 0
    _INSIDE = 1

    def __init__(
        self,
        ctx: FilterContext,
        *,
        filters: Optional[Iterable[ResponseFilter]] = None,
    ) -> None:
        self._ctx = ctx
        self._filters: tuple[ResponseFilter, ...] = (
            tuple(filters) if filters is not None else OUTBOUND_FILTERS
        )
        self._state = self._OUTSIDE
        self._pending = ""        # OUTSIDE: not-yet-flushed tail
        self._block_inner = ""    # INSIDE: accumulated inner content
        self._block_lang = ""     # INSIDE: current block language
        self._enabled = bool(self._filters)

    @property
    def has_open_block(self) -> bool:
        return self._state == self._INSIDE

    async def feed(self, chunk: str) -> str:
        """Push a chunk; return text safe to emit now (may be empty)."""
        if not self._enabled:
            return chunk
        if not chunk:
            return ""
        out: list[str] = []
        if self._state == self._OUTSIDE:
            self._pending += chunk
            await self._drain_outside(out)
        else:
            self._block_inner += chunk
            await self._drain_inside(out)
        return "".join(out)

    async def flush(self) -> str:
        """Emit any buffered tail. Call once at end-of-stream."""
        if not self._enabled:
            return ""
        out: list[str] = []
        if self._state == self._INSIDE:
            log.warning(
                "response_filter: stream ended inside fenced block "
                "(lang=%r); flushing %d chars unfiltered",
                self._block_lang,
                len(self._block_inner),
            )
            if self._block_inner:
                out.append(self._block_inner)
            self._block_inner = ""
            self._block_lang = ""
            self._state = self._OUTSIDE
        if self._pending:
            out.append(self._pending)
            self._pending = ""
        if _global_filters(self._filters):
            log.debug(
                "response_filter: %d global filter(s) skipped in streaming mode",
                len(_global_filters(self._filters)),
            )
        return "".join(out)

    # -- internal -----------------------------------------------------

    async def _drain_outside(self, out: list[str]) -> None:
        while True:
            nl = self._pending.find("\n")
            if nl < 0:
                if not _could_be_fence_prefix(self._pending):
                    if self._pending:
                        out.append(self._pending)
                        self._pending = ""
                return
            line = self._pending[: nl + 1]   # includes newline
            rest = self._pending[nl + 1 :]
            stripped = line.rstrip("\n").rstrip("\r")
            m = _FENCE_OPEN_RE.match(stripped)
            if m and _filters_for_block(m.group(1) or "", self._filters):
                out.append(line)             # echo opening fence
                self._block_lang = (m.group(1) or "").lower()
                self._block_inner = ""
                self._pending = ""
                self._state = self._INSIDE
                if rest:
                    self._block_inner += rest
                    await self._drain_inside(out)
                return
            out.append(line)
            self._pending = rest

    async def _drain_inside(self, out: list[str]) -> None:
        loc = _find_closing_fence(self._block_inner)
        if loc is None:
            return
        start, end = loc
        inner_text = self._block_inner[:start]
        close_text = self._block_inner[start:end]
        tail = self._block_inner[end:]
        transformed = await _run_block_filters(
            inner_text, self._block_lang, self._ctx, self._filters
        )
        out.append(transformed + close_text)
        self._block_inner = ""
        self._block_lang = ""
        self._state = self._OUTSIDE
        if tail:
            self._pending = tail
            await self._drain_outside(out)


async def wrap_stream(
    source: AsyncIterator[str],
    ctx: FilterContext,
    *,
    filters: Optional[Iterable[ResponseFilter]] = None,
) -> AsyncIterator[str]:
    """Async-iterator wrapper around :class:`StreamFilter`."""
    sf = StreamFilter(ctx, filters=filters)
    async for chunk in source:
        emit = await sf.feed(chunk)
        if emit:
            yield emit
    tail = await sf.flush()
    if tail:
        yield tail


def _could_be_fence_prefix(s: str) -> bool:
    """True if ``s`` could still grow into an opening-fence line."""
    tail = s.rsplit("\n", 1)[-1]
    if not tail:
        return False
    return re.fullmatch(r"`{0,3}[\w+-]*\s*", tail) is not None


def _find_closing_fence(buf: str) -> Optional[tuple[int, int]]:
    """Return ``(start, end)`` of a closing ``` fence line in ``buf``.

    The closing fence must be terminated by a newline. Returns ``None``
    if no confirmed closing line is in the buffer yet.
    """
    for m in _FENCE_CLOSE_RE.finditer(buf):
        end = m.end()
        if end >= len(buf):
            return None
        if buf[end] == "\n":
            return (m.start(), end + 1)
    return None
