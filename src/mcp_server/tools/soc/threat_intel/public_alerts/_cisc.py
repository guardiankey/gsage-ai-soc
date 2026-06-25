"""gSage AI — CISC (gov.br/cisc) public-alert source parser.

Parses the CISC cybersecurity alerts listing page at::

    https://www.gov.br/cisc/pt-br/alertas-de-ciberseguranca

The page is Plone-based — alerts are embedded in a
``<script type="application/json">`` block under the
``@navigation`` key.
"""

from __future__ import annotations

import logging
import re
from typing import ClassVar

from src.mcp_server.tools.soc.threat_intel.public_alerts._base import SourceParser

log = logging.getLogger(__name__)


class CISCParser(SourceParser):
    """Parser for CISC (gov.br/cisc) cybersecurity alerts.

    Extracts alerts from the Plone ``@navigation`` JSON embedded
    in the page's ``<script type="application/json">`` tag.
    """

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
        """Extract alerts from the Plone @navigation JSON.

        The full page JSON contains JS literals (``undefined``) and
        is not valid JSON.  We extract just the ``@navigation`` block
        via regex.  The alerts are also in static HTML as
        ``<a class="br-item">`` links with ``<div class="content">`` titles.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        items: list[dict] = []

        # Find the "Alertas de Cibersegurança" section header
        alertas_header = soup.find(
            "a", class_="br-item",
            href=lambda h: bool(h and "alertas-de-ciberseguranca" in h),
        )
        if not alertas_header:
            log.debug("CISC: alertas header not found")
            return []

        # The alert links are <a class="br-item"> with href deeper than the header
        parent = alertas_header.find_parent(["div", "nav", "section"])
        if not parent:
            parent = soup

        seen: set[str] = set()
        for link in parent.find_all("a", class_="br-item", href=True):
            href = str(link.get("href", ""))
            # Skip the header itself and non-alert links
            if not href.startswith("/cisc/pt-br/alertas-de-ciberseguranca/"):
                continue
            if href == "/cisc/pt-br/alertas-de-ciberseguranca":
                continue

            content_div = link.find("div", class_="content")
            title = content_div.get_text(strip=True) if content_div else link.get_text(strip=True)
            if not title or len(title) < 5:
                continue
            if title.lower() in seen:
                continue
            seen.add(title.lower())

            full_url = f"https://www.gov.br{href}"
            items.append({
                "title": title[:255],
                "url": full_url,
                "date_text": "",
                "summary": None,
            })

        log.debug("CISC: parsed %d alerts from br-item links", len(items))
        return items
