"""gSage AI — CAIS (rnp.br/cais) public-alert source parser.

Parses the CAIS announcements page at::

    https://www.rnp.br/cais/

The CAIS (Centro de Atendimento a Incidentes de Segurança) page lists
security announcements and alerts.  The information is in static HTML.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from bs4 import Tag

from src.mcp_server.tools.soc.threat_intel.public_alerts._base import SourceParser

log = logging.getLogger(__name__)


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
        """Parse the CAIS listing page.

        The CAIS/RPN page uses static HTML.  Alerts are typically listed
        as article entries, list items, or table rows with a title link.
        """
        from src.shared.http_utils import parse_html_dom

        soup = parse_html_dom(html)
        items: list[dict] = []

        content_area = (
            soup.find("div", id="content")
            or soup.find("div", class_="view-content")
            or soup.find("main")
            or soup.find("article")
            or soup
        )

        # RNP/CAIS typically uses Drupal: <div class="view-content">
        # with <div class="views-row"> entries or <article> tags.
        candidates = content_area.find_all(
            ["article", "div", "li", "tr"],
            class_=lambda c: bool(
                c and any(
                    w in str(c).lower()
                    for w in ("views-row", "node", "item", "entry", "result", "post")
                )
            ),
        )

        if not candidates:
            # Broader fallback: any block-level element with a child <a>
            candidates = []
            for tag in content_area.find_all(["div", "li", "article", "section"]):
                link = tag.find("a", href=True)
                if link and len(cls._safe_text(link)) > 15:
                    candidates.append(tag)

        for entry in candidates:
            link = entry.find("a", href=True)
            if not link:
                continue

            title = cls._safe_text(link)
            if not title or len(title) < 10:
                continue

            url = cls._safe_href(link, "https://www.rnp.br")
            if not url:
                continue

            # Find date — RNP often uses <span class="date"> or <time>
            date_text = ""
            time_tag = entry.find("time")
            if time_tag:
                date_text = time_tag.get("datetime") or cls._safe_text(time_tag)
            if not date_text:
                date_el = entry.find(
                    ["span", "div", "small"],
                    class_=lambda c: bool(c and "date" in str(c).lower()),
                )
                if date_el:
                    date_text = cls._safe_text(date_el)
            if not date_text:
                entry_text = cls._safe_text(entry)
                date_text = entry_text[:200]

            # Summary
            desc_el = entry.find(
                ["p", "div"],
                class_=lambda c: bool(
                    c and any(
                        w in str(c).lower()
                        for w in ("description", "body", "content", "teaser", "summary")
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

        log.debug("CAIS: parsed %d items from listing", len(unique))
        return unique
