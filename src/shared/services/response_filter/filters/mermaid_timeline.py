"""Rewrite ``HH:MM`` time labels in Mermaid ``timeline`` blocks.

Mermaid's ``timeline`` parser treats ``:`` as the event-text separator,
so a label like ``02:41 : Asset adicionado`` confuses it and the
diagram fails to render. Replacing the colon with ``h`` (and appending
``m``) keeps the visual meaning while side-stepping the parser quirk:

    02:41 : Asset adicionado   →   02h41m : Asset adicionado

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

# A bare HH:MM token at the start of a line, followed by whitespace then
# the mermaid event separator ``:``. The required whitespace before the
# second colon makes ``12:34:56`` (HH:MM:SS) NOT match — only true
# label-style ``HH:MM : description`` is rewritten.
_HHMM_RE = re.compile(r"^(\s*)([01]?\d|2[0-3]):([0-5]\d)(\s+:)")


class MermaidTimelineTimeFilter:
    """Convert ``HH:MM`` timeline labels to ``HHhMMm``.

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


def _rewrite_line(line: str) -> str:
    # Guard: leave HH:MM:SS alone — only matches when the char right
    # after the minutes is whitespace or the event separator ":".
    # The regex anchors to the first ``HH:MM`` token of the line and
    # requires it to be followed by optional spaces and a literal ``:``
    # (the timeline event separator), so descriptive ``12:34`` later in
    # the line is untouched.
    m = _HHMM_RE.match(line)
    if not m:
        return line
    leading, hh, mm, sep = m.group(1), m.group(2), m.group(3), m.group(4)
    new_token = f"{leading}{hh}h{mm}m{sep}"
    return new_token + line[m.end():]
