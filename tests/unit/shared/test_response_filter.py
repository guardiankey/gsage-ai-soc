"""Unit tests for the response filter pipeline."""
from __future__ import annotations

from typing import AsyncIterator, ClassVar
from uuid import uuid4

import pytest

from src.shared.services.response_filter import (
    FilterContext,
    StreamFilter,
    apply_filters_to_text,
    wrap_stream,
)
from src.shared.services.response_filter.base import Granularity, ResponseFilter
from src.shared.services.response_filter.filters.mermaid_timeline import (
    MermaidTimelineTimeFilter,
)


def _ctx() -> FilterContext:
    return FilterContext(org_id=uuid4(), interface="cli")


# ---------------------------------------------------------------------------
# MermaidTimelineTimeFilter — pure transform
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mermaid_timeline_basic_rewrite() -> None:
    f = MermaidTimelineTimeFilter()
    text = "timeline\n  title Events\n  02:41 : Asset adicionado\n  23:00 : Logout\n"
    out = await f.apply(text, _ctx())
    assert "02h41m : Asset adicionado" in out
    assert "23h00m : Logout" in out
    assert "02:41" not in out
    assert "23:00" not in out


@pytest.mark.asyncio
async def test_mermaid_timeline_preserves_inline_times() -> None:
    f = MermaidTimelineTimeFilter()
    text = "timeline\n  02:41 : score : 12:34 pontos\n"
    out = await f.apply(text, _ctx())
    assert "02h41m : score : 12:34 pontos" in out


@pytest.mark.asyncio
async def test_mermaid_timeline_ignores_non_timeline() -> None:
    f = MermaidTimelineTimeFilter()
    text = "graph TD\n  A --> B\n  02:41 : foo\n"
    assert await f.apply(text, _ctx()) == text


@pytest.mark.asyncio
async def test_mermaid_timeline_converts_seconds() -> None:
    """HH:MM:SS is now normalised to HHhMMmSSs."""
    f = MermaidTimelineTimeFilter()
    text = "timeline\n  12:34:56 : with seconds\n"
    out = await f.apply(text, _ctx())
    assert "12h34m56s : with seconds" in out


@pytest.mark.asyncio
async def test_mermaid_timeline_fixes_mixed_format() -> None:
    """HHhMM:SS (broken agent output) → HHhMMmSSs."""
    f = MermaidTimelineTimeFilter()
    text = "timeline\n  10h18:30 : conexao iniciada\n"
    out = await f.apply(text, _ctx())
    assert "10h18m30s : conexao iniciada" in out


@pytest.mark.asyncio
async def test_mermaid_timeline_adds_missing_m_suffix() -> None:
    """HHhMM without 'm' → HHhMMm."""
    f = MermaidTimelineTimeFilter()
    text = "timeline\n  08h01 : artefato executado\n"
    out = await f.apply(text, _ctx())
    assert "08h01m : artefato executado" in out


@pytest.mark.asyncio
async def test_mermaid_timeline_normalises_range() -> None:
    """HH:MM-HH:MM range → HHhMMm-HHhMMm."""
    f = MermaidTimelineTimeFilter()
    text = "timeline\n  08:01-12:00 : janela de tempo\n"
    out = await f.apply(text, _ctx())
    assert "08h01m-12h00m : janela de tempo" in out


@pytest.mark.asyncio
async def test_mermaid_timeline_normalises_mixed_range() -> None:
    """Mixed-format range HHhMM:SS-HHhMM:SS → HHhMMmSSs-HHhMMmSSs."""
    f = MermaidTimelineTimeFilter()
    text = "timeline\n  10h18:30-10h19:25 : trafego DNS\n"
    out = await f.apply(text, _ctx())
    assert "10h18m30s-10h19m25s : trafego DNS" in out


@pytest.mark.asyncio
async def test_mermaid_timeline_preserves_already_correct() -> None:
    """Already-correct format is left untouched."""
    f = MermaidTimelineTimeFilter()
    text = "timeline\n  10h18m30s : correto\n  08h01m-12h00m : range ok\n"
    out = await f.apply(text, _ctx())
    assert "10h18m30s : correto" in out
    assert "08h01m-12h00m : range ok" in out


@pytest.mark.asyncio
async def test_mermaid_timeline_with_leading_comment() -> None:
    f = MermaidTimelineTimeFilter()
    text = "%% comment\ntimeline\n  09:05 : evento\n"
    out = await f.apply(text, _ctx())
    assert "09h05m : evento" in out


@pytest.mark.asyncio
async def test_mermaid_timeline_empty() -> None:
    f = MermaidTimelineTimeFilter()
    assert await f.apply("", _ctx()) == ""


# ---------------------------------------------------------------------------
# apply_filters_to_text — full text mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_filters_full_text_rewrites_block() -> None:
    text = (
        "before\n"
        "```mermaid\n"
        "timeline\n"
        "  02:41 : foo\n"
        "```\n"
        "after\n"
    )
    out = await apply_filters_to_text(text, _ctx())
    assert "02h41m : foo" in out
    assert out.startswith("before\n")
    assert out.endswith("after\n")
    assert "```mermaid\n" in out
    assert "```\n" in out


@pytest.mark.asyncio
async def test_apply_filters_multiple_blocks() -> None:
    text = (
        "```mermaid\ntimeline\n  01:00 : a\n```\n"
        "mid\n"
        "```mermaid\ntimeline\n  02:00 : b\n```\n"
    )
    out = await apply_filters_to_text(text, _ctx())
    assert "01h00m : a" in out
    assert "02h00m : b" in out


@pytest.mark.asyncio
async def test_apply_filters_unterminated_block_passthrough() -> None:
    text = "```mermaid\ntimeline\n  02:41 : foo\n"
    out = await apply_filters_to_text(text, _ctx())
    # No closing fence → pipeline leaves it untouched.
    assert out == text


@pytest.mark.asyncio
async def test_apply_filters_no_blocks() -> None:
    text = "Just plain text with 02:41 timestamp.\n"
    out = await apply_filters_to_text(text, _ctx())
    assert out == text


@pytest.mark.asyncio
async def test_apply_filters_non_mermaid_block_untouched() -> None:
    text = "```sql\nSELECT 02:41 FROM t\n```\n"
    out = await apply_filters_to_text(text, _ctx())
    assert out == text


# ---------------------------------------------------------------------------
# StreamFilter / wrap_stream
# ---------------------------------------------------------------------------


async def _drain(chunks: list[str]) -> str:
    """Feed chunks one-by-one through a StreamFilter and return concat output."""
    sf = StreamFilter(_ctx())
    parts: list[str] = []
    for c in chunks:
        parts.append(await sf.feed(c))
    parts.append(await sf.flush())
    return "".join(parts)


@pytest.mark.asyncio
async def test_stream_passthrough_plain_text() -> None:
    out = await _drain(["hello ", "world\n", "more\n"])
    assert out == "hello world\nmore\n"


@pytest.mark.asyncio
async def test_stream_rewrites_timeline_block() -> None:
    chunks = [
        "intro\n",
        "```mermaid\n",
        "timeline\n",
        "  02:41 : foo\n",
        "  23:00 : bar\n",
        "```\n",
        "outro\n",
    ]
    out = await _drain(chunks)
    assert "02h41m : foo" in out
    assert "23h00m : bar" in out
    assert "intro\n" in out
    assert "outro\n" in out


@pytest.mark.asyncio
async def test_stream_split_fence_at_backticks() -> None:
    chunks = ["``", "`mermaid\n", "timeline\n", "  02:41 : x\n", "```\n"]
    out = await _drain(chunks)
    assert "```mermaid\n" in out
    assert "02h41m : x" in out


@pytest.mark.asyncio
async def test_stream_split_label_mid_token() -> None:
    chunks = ["```mermaid\ntimeline\n  02", ":41 : split\n```\n"]
    out = await _drain(chunks)
    assert "02h41m : split" in out


@pytest.mark.asyncio
async def test_stream_non_mermaid_block_passthrough() -> None:
    chunks = ["```sql\n", "SELECT 02:41\n", "```\n"]
    out = await _drain(chunks)
    assert out == "```sql\nSELECT 02:41\n```\n"


@pytest.mark.asyncio
async def test_stream_emits_text_before_block_eagerly() -> None:
    sf = StreamFilter(_ctx())
    early = await sf.feed("hello\n")
    assert early == "hello\n"
    rest = await sf.feed("```mermaid\ntimeline\n  02:41 : x\n```\n")
    rest += await sf.flush()
    assert "02h41m : x" in rest


@pytest.mark.asyncio
async def test_stream_unterminated_block_flushes_raw() -> None:
    sf = StreamFilter(_ctx())
    out = await sf.feed("```mermaid\ntimeline\n  02:41 : x\n")
    out += await sf.flush()
    # No closing fence — content is flushed unfiltered.
    assert "02:41" in out
    assert "02h41m" not in out


@pytest.mark.asyncio
async def test_stream_multiple_blocks_per_stream() -> None:
    chunks = [
        "```mermaid\ntimeline\n  01:00 : a\n```\n",
        "mid\n",
        "```mermaid\ntimeline\n  02:00 : b\n```\n",
    ]
    out = await _drain(chunks)
    assert "01h00m : a" in out
    assert "02h00m : b" in out


@pytest.mark.asyncio
async def test_wrap_stream_iterator() -> None:
    async def src() -> AsyncIterator[str]:
        for c in ["```mermaid\ntimeline\n", "  02:41 : foo\n", "```\n"]:
            yield c

    parts: list[str] = []
    async for piece in wrap_stream(src(), _ctx()):
        parts.append(piece)
    out = "".join(parts)
    assert "02h41m : foo" in out


# ---------------------------------------------------------------------------
# Granularity routing
# ---------------------------------------------------------------------------


class _SqlUpper:
    """Test filter: uppercase content of ```sql blocks."""

    name: ClassVar[str] = "sql_upper_test"
    granularity: ClassVar[Granularity] = "fenced_block:sql"

    async def apply(self, text: str, ctx: FilterContext) -> str:
        return text.upper()


class _GlobalSuffix:
    name: ClassVar[str] = "global_suffix_test"
    granularity: ClassVar[Granularity] = "global"

    async def apply(self, text: str, ctx: FilterContext) -> str:
        return text + "[END]"


@pytest.mark.asyncio
async def test_only_matching_block_filter_applies() -> None:
    text = "```sql\nselect 1\n```\n```mermaid\ntimeline\n  01:00 : a\n```\n"
    out = await apply_filters_to_text(
        text, _ctx(), filters=[_SqlUpper(), MermaidTimelineTimeFilter()]
    )
    assert "SELECT 1" in out
    assert "01h00m : a" in out


@pytest.mark.asyncio
async def test_global_filter_applied_in_full_text_mode() -> None:
    out = await apply_filters_to_text("hello\n", _ctx(), filters=[_GlobalSuffix()])
    assert out.endswith("[END]")


@pytest.mark.asyncio
async def test_global_filter_skipped_in_streaming_mode() -> None:
    sf = StreamFilter(_ctx(), filters=[_GlobalSuffix()])
    out = await sf.feed("hello\n")
    out += await sf.flush()
    assert out == "hello\n"


def _runtime_check_protocol() -> None:
    """Static sanity check: filters satisfy the ResponseFilter protocol."""
    assert isinstance(MermaidTimelineTimeFilter(), ResponseFilter)
    assert isinstance(_SqlUpper(), ResponseFilter)
    assert isinstance(_GlobalSuffix(), ResponseFilter)


def test_protocol_isinstance() -> None:
    _runtime_check_protocol()
