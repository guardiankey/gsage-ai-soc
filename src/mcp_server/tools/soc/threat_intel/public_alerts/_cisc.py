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
        entries with a title link and a date.  The gov.br platform may
        serve JS-rendered content — if the HTML is thin we won't find
        anything.
        """
        from src.shared.http_utils import parse_html_dom

        soup = parse_html_dom(html)
        items: list[dict] = []
        seen_titles: set[str] = set()

        content_area = (
            soup.find("div", id="content-core")
            or soup.find("article")
            or soup.find("main")
            or soup.find("div", id="content")
            or soup
        )

        # Strategy 1: gov.br tile pattern
        for entry in content_area.find_all(
            ["article", "div"],
            class_=lambda c: bool(
                c and any(
                    w in str(c).lower()
                    for w in ("tile", "entry", "item", "alert", "listing", "result", "row")
                )
            ),
        ):
            cls._extract_from_entry(entry, items, seen_titles)

        if items:
            log.debug("CISC: parsed %d items from tile selectors", len(items))
            return items

        # Strategy 2: <li> elements with links in content area
        for li in content_area.find_all("li"):
            link = li.find("a", href=True)
            if not link or len(cls._safe_text(link)) < 15:
                continue
            cls._extract_from_entry(li, items, seen_titles)

        if items:
            log.debug("CISC: parsed %d items from <li> fallback", len(items))
            return items

        # Strategy 3: any substantial <a> in content area
        candidate_tags: list[Tag] = []
        for a in content_area.find_all("a", href=True):
            text = cls._safe_text(a)
            href = str(a.get("href", ""))
            if len(text) < 15:
                continue
            if href.startswith("mailto:") or href.startswith("javascript:"):
                continue
            if any(skip in href.lower() for skip in ("/login", "/acessibilidade", "/menu", "/rodape")):
                continue
            parent = a.find_parent(["article", "li", "div", "p", "section"])
            candidate_tags.append(parent or a)

        seen_tags: set[int] = set()
        for tag in candidate_tags:
            if id(tag) in seen_tags:
                continue
            seen_tags.add(id(tag))
            cls._extract_from_entry(tag, items, seen_titles)

        log.debug(
            "CISC: parsed %d items from broad <a> fallback "
            "(html_len=%d, content_area=%s)",
            len(items), len(html),
            content_area.name if content_area else "none",
        )
        return items

    @classmethod
    def _extract_from_entry(
        cls, entry: Tag, items: list[dict], seen_titles: set[str],
    ) -> None:
        """Extract alert data from a candidate entry element."""
        link = entry.find("a", href=True)
        if not link:
            return

        title = cls._safe_text(link)
        if not title or len(title) < 10:
            return
        if title.lower() in seen_titles:
            return
        seen_titles.add(title.lower())

        url = cls._safe_href(link, "https://www.gov.br")
        if not url:
            return

        # Find date
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
            entry_text = cls._safe_text(entry)
            from src.shared.http_utils import parse_date_br
            for word in entry_text.split():
                if parse_date_br(word):
                    date_text = word
                    break
            if not date_text:
                date_text = entry_text[:200]

        # Summary
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
