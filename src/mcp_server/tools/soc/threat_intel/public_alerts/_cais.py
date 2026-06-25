"""gSage AI — CAIS (rnp.br/cais) public-alert source parser.

Parses the CAIS announcements at::

    https://www.rnp.br/cais/

Alerts are listed at the bottom of the institutional page under the
**"Sistema de Alerta do Cais"** heading.  Each entry follows the pattern::

    CAIS-Alerta DD/MM/AAAA: <title>
    [baixar txt](<url to .txt file>)

The ``content_url`` points to the raw ``.txt`` file (unwrapped from the
Google Docs viewer if necessary).
"""

from __future__ import annotations

import logging
import re
from typing import ClassVar
from urllib.parse import unquote

from src.mcp_server.tools.soc.threat_intel.public_alerts._base import SourceParser

log = logging.getLogger(__name__)

# Pattern: "CAIS-Alerta" followed by optional date and title.
# Supported date formats:
#   CAIS-Alerta DD/MM/AAAA: title
#   CAIS-Alerta [DD-MM-AAAA]: title
#   CAIS-Alerta: title (no date)
#
# The title capture is non-greedy — stops before the next "CAIS-Alerta",
# "baixar txt", or end of string.
_CAIS_ALERTA_RE = re.compile(
    r"CAIS[-\s]Alerta\s*"
    r"(?:"
    r"\[?(\d{1,2}/\d{1,2}/\d{4})\]?"    # DD/MM/AAAA  (opt bracketed)
    r"|"
    r"\[(\d{1,2}-\d{1,2}-\d{4})\]"      # [DD-MM-AAAA]
    r")?"
    r"\s*[:–-]\s*"
    r"(.+?)"                              # title (non-greedy)
    r"(?=\s*(?:CAIS[-\s]Alerta|baixar\s+txt|Sistema\s+de\s+Alerta|$))",
    re.IGNORECASE | re.DOTALL,
)

# Google Docs viewer URL pattern — extract the real .txt URL
_GVIEW_RE = re.compile(
    r"url=(https?%3A%2F%2F[^&]+)",
    re.IGNORECASE,
)


class CAISParser(SourceParser):
    """Parser for CAIS (rnp.br/cais) security announcements."""

    source_id: ClassVar[str] = "cais"
    source_name: ClassVar[str] = "CAIS"
    source_full_name: ClassVar[str] = (
        "CAIS — Centro de Atendimento a Incidentes de Segurança "
        "(rnp.br/cais)"
    )
    list_url: ClassVar[str] = "https://www.rnp.br/cais/"
    update_frequency: ClassVar[str] = "daily"

    @classmethod
    def _parse_list_items(cls, html: str) -> list[dict]:
        """Parse the CAIS page for 'CAIS-Alerta' entries.

        The page is WordPress/Divi — the heading and alerts live in
        separate blocks.  We scan the full visible text for the
        ``CAIS-Alerta`` pattern and match each entry with the nearest
        ``[baixar txt]`` link.
        """
        from src.shared.http_utils import parse_html_dom

        soup = parse_html_dom(html)
        full_text = soup.get_text(" ", strip=True)

        # ── Find the "Sistema de Alerta" section ────────────────────────
        idx = full_text.lower().find("sistema de alerta")
        if idx == -1:
            log.debug("CAIS: 'Sistema de Alerta' not found in page text")
            return []
        section_text = full_text[idx:]

        # ── Find all "baixar txt" links in document order ───────────────
        download_links: list[dict] = []
        for a in soup.find_all("a", href=True):
            link_text = (a.get_text() or "").strip().lower()
            if "baixar" in link_text:
                download_links.append({
                    "href": str(a.get("href", "")),
                    "text": a.get_text(strip=True),
                })

        # ── Find all CAIS-Alerta entries in the section text ────────────
        items: list[dict] = []
        for m in _CAIS_ALERTA_RE.finditer(section_text):
            # Group 1 = DD/MM/AAAA, Group 2 = [DD-MM-AAAA], Group 3 = title
            date_text = m.group(1) or m.group(2) or ""
            # Normalise bracketed date to slash format for parse_date_br
            if date_text and "-" in date_text and "/" not in date_text:
                date_text = date_text.replace("-", "/")
            title = (m.group(3) or "").strip()[:255]

            # Match with the next available download link (order-preserving)
            link = download_links[len(items)] if len(items) < len(download_links) else None
            real_url = cls._unwrap_url(link["href"]) if link else ""
            items.append({
                "title": f"CAIS-Alerta {date_text}: {title}" if date_text else f"CAIS-Alerta: {title}",
                "url": real_url or (link["href"] if link else ""),
                "date_text": date_text,
                "summary": title[:500],
            })

        log.debug("CAIS: parsed %d alerts from 'Sistema de Alerta'", len(items))
        return items

    @staticmethod
    def _unwrap_url(url: str) -> str:
        """Extract the real .txt URL from a Google Docs viewer wrapper.

        ``https://docs.google.com/viewerng/viewer?url=https%3A//...txt...``
        → ``https://plataforma.rnp.br/...txt``
        """
        if "docs.google.com/viewer" not in url:
            return url
        m = _GVIEW_RE.search(url)
        if m:
            return unquote(m.group(1))
        return url
