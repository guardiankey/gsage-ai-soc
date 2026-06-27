"""gSage AI — CTIR (gov.br/ctir) public-alert source parser.

Parses both **alertas** and **recomendações** from the CTIR/Gov pages::

    https://www.gov.br/gsi/pt-br/assuntos/ctir/alertas/{ano}
    https://www.gov.br/ctir/pt-br/assuntos/alertas-e-recomendacoes/recomendacoes/{ano}

Each item is tagged with a ``category`` field: ``"alerta"`` or ``"recomendação"``.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import ClassVar

from bs4 import Tag

from src.mcp_server.tools.soc.threat_intel.public_alerts._base import SourceParser

log = logging.getLogger(__name__)


class CTIRParser(SourceParser):
    """Parser for CTIR (gov.br/ctir) alerts and recommendations."""

    source_id: ClassVar[str] = "ctir"
    source_name: ClassVar[str] = "CTIR"
    source_full_name: ClassVar[str] = (
        "CTIR — Centro de Tratamento e Resposta a Incidentes Cibernéticos "
        "(gov.br/ctir)"
    )
    # ── Recommendations (gov.br/ctir domain) ────────────────────────────
    list_url: ClassVar[str] = (
        "https://www.gov.br/ctir/pt-br/assuntos/alertas-e-recomendacoes"
        f"/recomendacoes/{date.today().year}"
    )
    _RECS_FALLBACK_URL: ClassVar[str] = (
        "https://www.gov.br/ctir/pt-br/assuntos/alertas-e-recomendacoes"
        f"/recomendacoes/{date.today().year - 1}"
    )
    # ── Alerts (gov.br/gsi domain — publicly accessible) ────────────────
    _ALERTAS_URL: ClassVar[str] = (
        "https://www.gov.br/gsi/pt-br/assuntos/ctir/alertas"
        f"/{date.today().year}"
    )
    _ALERTAS_FALLBACK_URL: ClassVar[str] = (
        "https://www.gov.br/gsi/pt-br/assuntos/ctir/alertas"
        f"/{date.today().year - 1}"
    )

    update_frequency: ClassVar[str] = "daily"

    # ── Public API ────────────────────────────────────────────────────────

    @classmethod
    async def fetch_and_parse(cls, *, max_results: int = 10, timeout: float = 30.0) -> list[dict]:
        """Fetch from **both** alerta and recomendação sources.

        Returns up to *max_results* items merged and sorted by
        ``published_at`` descending (newest first).
        """
        all_items: list[dict] = []

        # Recommendations
        try:
            recs = await cls._fetch_source(
                url=cls.list_url,
                fallback_url=cls._RECS_FALLBACK_URL,
                category="recomendação",
                timeout=timeout,
            )
            all_items.extend(recs)
        except Exception:
            log.exception("CTIR: failed to fetch recommendations")

        # Alerts
        try:
            alerts = await cls._fetch_source(
                url=cls._ALERTAS_URL,
                fallback_url=cls._ALERTAS_FALLBACK_URL,
                category="alerta",
                timeout=timeout,
            )
            all_items.extend(alerts)
        except Exception:
            log.exception("CTIR: failed to fetch alerts")

        if not all_items:
            return []

        # Normalize all items together
        normalized = cls._normalize(all_items)
        normalized.sort(key=lambda a: a["published_at"], reverse=True)
        return normalized[:max_results]

    # ── Internal helpers ──────────────────────────────────────────────────

    @classmethod
    async def _fetch_source(
        cls,
        *,
        url: str,
        fallback_url: str,
        category: str,
        timeout: float,
    ) -> list[dict]:
        """Fetch one CTIR source page, falling back to *fallback_url*.

        Each parsed item is tagged with the given *category*.
        """
        original = cls.list_url
        cls.list_url = url
        try:
            html = await cls._fetch_list(timeout=timeout)
            items = cls._parse_list_items(html, category=category)
            if items:
                return items
        except Exception:
            log.info("CTIR %s: primary URL failed, trying fallback", category)

        cls.list_url = fallback_url
        try:
            html = await cls._fetch_list(timeout=timeout)
            return cls._parse_list_items(html, category=category)
        finally:
            cls.list_url = original

    # ── Normalisation override ─────────────────────────────────────────────

    @classmethod
    def _normalize(cls, items: list[dict]) -> list[dict]:
        """Normalise items, preserving the ``category`` field."""
        # Collect extra fields keyed by (title, url) for lookup after
        # base-class normalisation strips them.
        extra_lookup: dict[tuple[str, str | None], dict] = {}
        for item in items:
            extra_lookup[(item.get("title", ""), item.get("url"))] = {
                "category": item.get("category"),
            }

        alerts = super()._normalize(items)

        for alert in alerts:
            key = (alert["title"], alert.get("content_url"))
            extras = extra_lookup.get(key, {})
            if extras.get("category"):
                alert["category"] = extras["category"]

        return alerts

    # ── Page parser ───────────────────────────────────────────────────────

    @classmethod
    def _parse_list_items(cls, html: str, category: str = "recomendação") -> list[dict]:
        """Parse a CTIR listing page (alertas or recomendações).

        Both pages use a similar Plone-based structure::

            <a href=".../alerta-XX-ano">ALERTA XX/ANO</a>
            — última modificação DD/MM/AAAA HHhMM

        or::

            <a href=".../recomendacao-XX-ano">RECOMENDAÇÃO XX/ANO</a>
            — última modificação DD/MM/AAAA HHhMM

        Parameters
        ----------
        html:
            The raw page HTML.
        category:
            ``"alerta"`` or ``"recomendação"`` — injected into every item's
            ``category`` key for downstream use.
        """
        from src.shared.http_utils import parse_html_dom

        soup = parse_html_dom(html)
        items: list[dict] = []

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

            # Skip navigation / utility links that happen to have long text.
            # The CTIR domain (/ctir/pt-br/…) is the *only* navigation prefix
            # we skip; links under /gsi/pt-br/… are actual alert URLs.
            skip_prefixes = ("/login", "javascript:", "#")
            if any(href.startswith(p) for p in skip_prefixes):
                continue
            # Also skip the CTIR domain nav links when the text is generic
            if href.startswith("/ctir/pt-br") and (
                len(text) < 10
                or any(
                    word in text.lower()
                    for word in ("acessibilidade", "conteúdo", "menu", "rodapé")
                )
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
            full_text: str = ""
            if parent:
                full_text = cls._safe_text(parent)
                if full_text and len(full_text) > len(text) + 10:
                    summary = cls._clean_summary_raw(full_text)

            # CTIR summaries contain "última modificação DD/MM/AAAA HHhMM".
            # Extract that date as the authoritative date for this item
            # (the parent-text date may be shared across items in a list).
            refined_date = cls._extract_ctir_date(summary) or cls._extract_ctir_date(full_text)
            if refined_date:
                date_text = refined_date or date_text

            items.append({
                "title": text.strip()[:255],
                "url": url,
                "date_text": date_text,
                "summary": summary,
                "category": category,
            })

        # Deduplicate by title
        seen: set[str] = set()
        unique: list[dict] = []
        for item in items:
            key = item["title"].lower()
            if key not in seen:
                seen.add(key)
                unique.append(item)

        log.debug("CTIR: parsed %d %s items from listing", len(unique), category)
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

    @staticmethod
    def _clean_summary_raw(text: str) -> str:
        """Collapse whitespace in CTIR listing text (before normalisation)."""
        import re
        return re.sub(r"\s+", " ", text).strip()[:500]

    @staticmethod
    def _extract_ctir_date(text: str | None) -> str | None:
        """Extract a BR date from CTIR's 'última modificação' pattern.

        ``"última modificação 15/01/2026 17h34"`` → ``"15/01/2026"``.
        """
        if not text:
            return None
        import re
        m = re.search(r"(?:última|ultima)\s+modificação\s+(\d{1,2}/\d{1,2}/\d{4})", text, re.IGNORECASE)
        return m.group(1) if m else None
