"""Rewrite time labels in Mermaid ``timeline`` blocks to avoid ``:``.

Mermaid's ``timeline`` parser treats ``:`` as the event-text separator,
so any colon inside the time label confuses it and the diagram fails to
render.  This filter normalises all common time formats to a ``h``/``m``/``s``
suffix style that carries the same visual meaning without colons:

    ┌──────────────────────────────────────┬─────────────────────────────────┐
    │ Input format                         │ Normalised output               │
    ├──────────────────────────────────────┼─────────────────────────────────┤
    │ ``02:41``                            │ ``02h41m``                      │
    │ ``12:34:56``                         │ ``12h34m56s``                   │
    │ ``10h18:30``       (broken mix)      │ ``10h18m30s``                   │
    │ ``08h01``          (missing ``m``)   │ ``08h01m``                      │
    │ ``10:00-12:00``     (range)          │ ``10h00m-12h00m``               │
    │ ``10h18:30-10h19:25``                │ ``10h18m30s-10h19m25s``         │
    │ ``2026-07-10 12:38 UTC`` (date+time) │ ``2026-07-10 12h38m UTC``      │
    │ ``2026-07-11 11:05-13:22 UTC``       │ ``2026-07-11 11h05m-13h22m UTC``│
    │ ``2026-07-02``        (date only)    │ ``2026-07-02`` (unchanged)      │
    └──────────────────────────────────────┴─────────────────────────────────┘

Only labels that appear as the **first non-whitespace token of a line,
before the first event separator** are rewritten. Times that occur in
the descriptive part (e.g. ``score : 12:34 pontos``) are preserved.
"""
from __future__ import annotations

import re
from typing import ClassVar

from src.shared.services.response_filter.base import (
    FilterContext,
    Granularity,
)

# Matches a timeline data line: leading whitespace, a time specification
# (any text), then the event separator `` : `` (space-colon-space).
# The lazy ``.+?`` ensures we stop at the *first* `` : `` on the line,
# so colons inside event text are never treated as separators.
# Group 1 = leading whitespace   Group 2 = time spec
# Group 3 = event separator      Group 4 = rest of line (event text)
_TIME_LINE_RE = re.compile(
    r"^(\s*)"   # leading whitespace
    r"(.+?)"    # time spec — lazy, stops at first " : "
    r"(\s+:)"   # event separator (spaces then colon)
    r"(.*)$"    # rest of line
)

# Regex substitutions applied *within* a time spec to normalise time patterns.
# Word boundaries (``\b``) ensure we only touch standalone time tokens,
# never substrings inside dates (``2026-07-10``) or other text.
# Applied in order; each handles a specific format.
_TIME_PATTERNS: list[tuple[str, str]] = [
    # HH:MM:SS → HHhMMmSSs  (must run before HH:MM)
    (r"\b(\d{1,2}):(\d{2}):(\d{2})\b", r"\1h\2m\3s"),
    # HHhMM:SS → HHhMMmSSs  (broken mixed format — agent mistake)
    (r"\b(\d{1,2})h(\d{2}):(\d{2})\b", r"\1h\2m\3s"),
    # HH:MM → HHhMMm  (only when NOT followed by :\d\d — i.e. not HH:MM:SS)
    (r"\b(\d{1,2}):(\d{2})\b(?!:\d)", r"\1h\2m"),
    # HHhMM → HHhMMm  (missing 'm' suffix; only when NOT already followed by 'm')
    (r"\b(\d{1,2})h(\d{2})\b(?!m)", r"\1h\2m"),
]


class MermaidTimelineTimeFilter:
    """Normalise time labels in Mermaid ``timeline`` blocks.

    Granularity ``fenced_block:mermaid`` — the pipeline hands us the
    inner content of every ` ```mermaid ... ``` ` block. We only act
    when the block is a ``timeline`` diagram.
    """

    name: ClassVar[str] = "mermaid_timeline_time_format"
    granularity: ClassVar[Granularity] = "fenced_block:mermaid"

    async def apply(self, text: str, ctx: FilterContext) -> str:
        if not text:
            return text
        # Detect "timeline" diagram type: first non-empty, non-comment line.
        first = _first_meaningful_line(text)
        if first is None or first.strip().lower() != "timeline":
            return text

        out_lines: list[str] = []
        for line in text.splitlines(keepends=True):
            out_lines.append(_rewrite_line(line))
        return "".join(out_lines)


def _first_meaningful_line(text: str) -> str | None:
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("%%"):  # mermaid comments
            continue
        return raw
    return None


def _normalize_time_spec(spec: str) -> str:
    """Normalise time patterns *within* a time specification.

    Unlike the previous tokeniser approach, this applies regex
    substitutions directly to the full spec string, so surrounding
    text (dates, timezone labels, plain words) is preserved untouched.
    """
    for pattern, replacement in _TIME_PATTERNS:
        spec = re.sub(pattern, replacement, spec)
    return spec


def _rewrite_line(line: str) -> str:
    """Rewrite a timeline data line, normalising the time label."""
    m = _TIME_LINE_RE.match(line)
    if not m:
        return line
    leading = m.group(1)
    time_spec = m.group(2)
    event_sep = m.group(3)
    rest = m.group(4)

    # Only normalise if the spec looks time-like (contains a digit).
    if not re.search(r"\d", time_spec):
        return line

    new_spec = _normalize_time_spec(time_spec)
    # Preserve anything after the match (e.g. trailing newline).
    return f"{leading}{new_spec}{event_sep}{rest}{line[m.end():]}"
