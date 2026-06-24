"""gSage AI — CTIR (gov.br/ctir) public-alert source parser.

Parses the CTIR recommendations listing page at::

    https://www.gov.br/ctir/pt-br/assuntos/alertas-e-recomendacoes/recomendacoes/{ano}

The page lists security recommendations by year.  Each entry typically
appears as a link (``<a>``) inside a list or table row, with the
recommendation title and publication date.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import ClassVar

from bs4 import Tag

from src.mcp_server.tools.soc.threat_intel.public_alerts._base import SourceParser

log = logging.getLogger(__name__)


class CTIRParser(SourceParser):
    """Parser for CTIR (gov.br/ctir) recommendations."""

    source_id: ClassVar[str] = "ctir"
    source_name: ClassVar[str] = "CTIR"
    source_full_name: ClassVar[str] = (
        "CTIR — Centro de Tratamento e Resposta a Incidentes Cibernéticos "
        "(gov.br/ctir)"
    )
    list_url: ClassVar[str] = (
        "https://www.gov.br/ctir/pt-br/assuntos/alertas-e-recomendacoes"
        f"/recomendacoes/{date.today().year}"
    )
    update_frequency: ClassVar[str] = "daily"

    # Fallback year if current year page is empty / not found
    _FALLBACK_URL: ClassVar[str] = (
        "https://www.gov.br/ctir/pt-br/assuntos/alertas-e-recomendacoes"
        f"/recomendacoes/{date.today().year - 1}"
    )

    @classmethod
    async def fetch_and_parse(cls, *, max_results: int = 10, timeout: float = 30.0) -> list[dict]:
        """Try current year first; fall back to previous year."""
        original_url = cls.list_url
        try:
            result = await super().fetch_and_parse(
                max_results=max_results, timeout=timeout
            )
            if result:
                return result
        except Exception:
            log.warning("CTIR: current year page failed, trying previous year")
        # Fallback
        cls.list_url = cls._FALLBACK_URL
        try:
            return await super().fetch_and_parse(
                max_results=max_results, timeout=timeout
            )
        finally:
            cls.list_url = original_url

    @classmethod
    def _parse_list_items(cls, html: str) -> list[dict]:
        """Parse the CTIR listing page.

        The page structure varies, but typically recommendations are
        ``<a>`` links with a date nearby.  We use BeautifulSoup to
        extract all plausible alert entries.
        """
        from src.shared.http_utils import parse_html_dom

        soup = parse_html_dom(html)
        items: list[dict] = []

        # Strategy 1: Look for <a> tags that look like alert links.
        # CTIR recommendations typically have URLs like
        # /ctir/pt-br/assuntos/.../recomendacoes/... or link to PDFs.
        content_area = (
            soup.find("div", id="content-core")
            or soup.find("article")
            or soup.find("main")
            or soup
        )

        links = content_area.find_all("a", href=True)
        for link in links:
            href = str(link.get("href", ""))
            text = cls._safe_text(link)
            if not text or len(text) < 10:
                continue
            # Skip navigation / utility links
            skip_prefixes = ("/ctir/pt-br", "/login", "javascript:", "#")
            if any(href.startswith(p) for p in skip_prefixes):
                # Only skip if the text looks generic / navigational
                if len(text) < 10 or any(
                    word in text.lower()
                    for word in ("acessibilidade", "conteúdo", "menu", "rodapé")
                ):
                    continue

            # Try to find a date sibling or parent text
            parent = link.find_parent(["li", "tr", "div", "td", "article"])
            parent_text = cls._safe_text(parent) if parent else text

            # Extract date from the surrounding context
            date_text = (
                cls._find_date_near(link)
                or cls._find_date_in_text(parent_text)
                or cls._find_date_in_text(text)
                or ""
            )

            # Build URL — resolve relative to gov.br
            url = cls._safe_href(link, "https://www.gov.br")
            if not url:
                continue

            # Extract a summary snippet from parent context
            summary = None
            if parent:
                full_text = cls._safe_text(parent)
                if full_text and len(full_text) > len(text) + 10:
                    summary = full_text[:500]

            items.append({
                "title": text.strip()[:255],
                "url": url,
                "date_text": date_text,
                "summary": summary,
            })

        # Deduplicate by title
        seen: set[str] = set()
        unique: list[dict] = []
        for item in items:
            key = item["title"].lower()
            if key not in seen:
                seen.add(key)
                unique.append(item)

        log.debug("CTIR: parsed %d items from listing", len(unique))
        return unique

    @classmethod
    def _find_date_near(cls, tag: Tag) -> str | None:
        """Look for a date string near *tag*."""
        # Check preceding / following siblings
        for sibling in tag.find_previous_siblings(["span", "small", "time", "p"]):
            text = cls._safe_text(sibling)
            if text and any(c.isdigit() for c in text):
                return text
        for sibling in tag.find_next_siblings(["span", "small", "time", "p"]):
            text = cls._safe_text(sibling)
            if text and any(c.isdigit() for c in text):
                return text
        # Check parent's direct text
        parent = tag.find_parent(["li", "td", "div"])
        if parent:
            return cls._safe_text(parent)
        return None

    @classmethod
    def _find_date_in_text(cls, text: str) -> str | None:
        """Look for a date pattern in arbitrary text."""
        from src.shared.http_utils import parse_date_br
        if parse_date_br(text):
            return text
        return None
