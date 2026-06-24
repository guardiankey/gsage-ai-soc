"""gSage AI — CISC (gov.br/cisc) public-alert source parser.

Parses the CISC cybersecurity alerts listing page at::

    https://www.gov.br/cisc/pt-br/alertas-de-ciberseguranca

The page lists published cybersecurity alerts with title, date, and a link
to the individual alert page.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from bs4 import Tag

from src.mcp_server.tools.soc.threat_intel.public_alerts._base import SourceParser

log = logging.getLogger(__name__)


class CISCParser(SourceParser):
    """Parser for CISC (gov.br/cisc) cybersecurity alerts."""

    source_id: ClassVar[str] = "cisc"
    source_name: ClassVar[str] = "CISC"
    source_full_name: ClassVar[str] = (
        "CISC — Centro Integrado de Segurança Cibernética "
        "(gov.br/cisc)"
    )
    list_url: ClassVar[str] = (
        "https://www.gov.br/cisc/pt-br/alertas-de-ciberseguranca"
    )
    update_frequency: ClassVar[str] = "daily"

    @classmethod
    def _parse_list_items(cls, html: str) -> list[dict]:
        """Parse the CISC listing page.

        CISC alerts are typically listed as ``<article>`` or ``<div>``
        entries with a title link and a date.
        """
        from src.shared.http_utils import parse_html_dom

        soup = parse_html_dom(html)
        items: list[dict] = []

        # Strategy 1: Look for article/div tiles with summary class patterns
        content_area = (
            soup.find("div", id="content-core")
            or soup.find("article")
            or soup.find("main")
            or soup
        )

        # Common gov.br pattern: <article class="tileItem"> or similar
        candidates = content_area.find_all(
            ["article", "div", "li"],
            class_=lambda c: bool(
                c and any(
                    w in str(c).lower()
                    for w in ("tile", "entry", "item", "alert", "listing", "result")
                )
            ),
        )

        if not candidates:
            # Fallback: find any <a> links with substantial text
            candidates = [
                a.find_parent(["article", "li", "div"])
                for a in content_area.find_all("a", href=True)
                if len(cls._safe_text(a)) > 15
            ]
            # Flatten and deduplicate
            seen_tags: set[int] = set()
            unique_candidates: list = []
            for c in candidates:
                if c and id(c) not in seen_tags:
                    seen_tags.add(id(c))
                    unique_candidates.append(c)
            candidates = unique_candidates

        for entry in candidates:
            # Find the title link
            link = entry.find("a", href=True)
            if not link:
                link = entry.find_parent("a", href=True)
            if not link:
                continue

            title = cls._safe_text(link)
            if not title or len(title) < 10:
                continue

            url = cls._safe_href(link, "https://www.gov.br")
            if not url:
                continue

            # Find date — look for time, span.date, or text with digits
            date_text = ""
            time_tag = entry.find("time")
            if time_tag:
                date_text = time_tag.get("datetime") or cls._safe_text(time_tag)
            if not date_text:
                date_el = entry.find(
                    ["span", "small", "p", "div"],
                    class_=lambda c: bool(c and "date" in str(c).lower()),
                )
                if date_el:
                    date_text = cls._safe_text(date_el)
            if not date_text:
                # Search entry text for date patterns
                entry_text = cls._safe_text(entry)
                from src.shared.http_utils import parse_date_br
                for word in entry_text.split():
                    if parse_date_br(word):
                        date_text = word
                        break
                if not date_text:
                    date_text = entry_text[:200]

            # Summary — use description text if available
            desc_el = entry.find(
                ["p", "div", "span"],
                class_=lambda c: bool(
                    c and any(
                        w in str(c).lower()
                        for w in ("description", "summary", "abstract", "body")
                    )
                ),
            )
            summary = cls._safe_text(desc_el)[:500] if desc_el else None

            items.append({
                "title": title.strip()[:255],
                "url": url,
                "date_text": date_text,
                "summary": summary,
            })

        # Deduplicate
        seen: set[str] = set()
        unique: list[dict] = []
        for item in items:
            key = item["title"].lower()
            if key not in seen:
                seen.add(key)
                unique.append(item)

        log.debug("CISC: parsed %d items from listing", len(unique))
        return unique
