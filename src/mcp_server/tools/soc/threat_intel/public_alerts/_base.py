"""gSage AI — Base SourceParser for public security alert sources.

Each source (CTIR, CISC, CAIS, …) subclasses ``SourceParser`` and
implements ``parse_list()`` to extract alert items from the listing HTML.
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup, Tag

from src.shared.http_utils import fetch_url, parse_date_br, parse_html_dom

log = logging.getLogger(__name__)


class SourceParser(ABC):
    """Base class for a public-alert source parser.

    Subclasses must set the class-level attributes and implement
    ``parse_list()``.
    """

    # ── Class attributes (override in subclass) ──────────────────────────
    source_id: str = ""
    source_name: str = ""
    source_full_name: str = ""
    list_url: str = ""
    update_frequency: str = "daily"  # daily | weekly

    # ── Public API ────────────────────────────────────────────────────────

    @classmethod
    async def fetch_and_parse(
        cls,
        *,
        max_results: int = 10,
        timeout: float = 30.0,
    ) -> list[dict]:
        """Fetch the listing page and return up to *max_results* parsed alerts.

        Returns a list of ``PublicAlert``-compatible dicts, ordered by
        ``published_at`` descending (most recent first).
        """
        html = await cls._fetch_list(timeout=timeout)
        items = cls._parse_list_items(html)
        alerts = cls._normalize(items)
        return alerts[:max_results]

    @classmethod
    async def _fetch_list(cls, timeout: float = 30.0) -> str:
        """Fetch the listing page HTML."""
        result = await fetch_url(
            cls.list_url,
            timeout=timeout,
            follow_redirects=True,
        )
        if result["status_code"] != 200:
            raise RuntimeError(
                f"{cls.source_id}: HTTP {result['status_code']} "
                f"fetching {cls.list_url}"
            )
        html = result["body"].decode("utf-8", errors="replace")
        log.debug("%s: fetched listing (%d bytes)", cls.source_id, len(html))
        return html

    # ── Subclass interface ────────────────────────────────────────────────

    @classmethod
    @abstractmethod
    def _parse_list_items(cls, html: str) -> list[dict]:
        """Parse the listing HTML and return raw alert items.

        Each item must be a dict with at least:
        ``{"title": str, "url": str | None, "date_text": str}``.

        ``date_text`` is a free-form date string that will be parsed by
        ``parse_date_br()`` during normalization.
        """
        ...

    @classmethod
    def _normalize(cls, items: list[dict]) -> list[dict]:
        """Normalize raw items into the ``PublicAlert`` schema.

        Override in subclass if the source needs custom normalization
        (e.g. to resolve relative URLs, extract severity, etc.).
        """
        alerts: list[dict] = []
        for item in items:
            pub_date = parse_date_br(item.get("date_text", ""))
            if pub_date is None:
                # Try ISO format as fallback
                try:
                    pub_date = date.fromisoformat(item["date_text"][:10])
                except (ValueError, KeyError):
                    pub_date = date.today()

            alert = {
                "id": cls._make_id(item.get("title", ""), pub_date),
                "source": cls.source_id,
                "title": item.get("title", ""),
                "content_url": item.get("url"),
                "severity": item.get("severity"),
                "published_at": pub_date.isoformat(),
                "summary": item.get("summary"),
                "categories": item.get("categories") or [],
                "tlp": "TLP:WHITE",
            }
            alerts.append(alert)

        alerts.sort(key=lambda a: a["published_at"], reverse=True)
        return alerts

    # ── Helpers ───────────────────────────────────────────────────────────

    @classmethod
    def _make_id(cls, title: str, pub_date: date) -> str:
        """Produce a deterministic, unique alert ID."""
        slug = (
            title.lower()
            .replace(" ", "-")
            .replace("/", "-")
            .replace(":", "")
        )[:60]
        return f"{cls.source_id}:{slug}:{pub_date.isoformat()}"

    @classmethod
    def _safe_text(cls, tag: Tag | None) -> str:
        """Extract stripped text from a BeautifulSoup Tag, or ''."""
        if tag is None:
            return ""
        return tag.get_text(" ", strip=True)

    @classmethod
    def _safe_href(cls, tag: Tag | None, base_url: str = "") -> Optional[str]:
        """Extract href from an <a> tag, resolving relative URLs."""
        if tag is None:
            return None
        href = tag.get("href")
        if not href or isinstance(href, list):
            return None
        href = str(href).strip()
        if href.startswith("http"):
            return href
        if href.startswith("/") and base_url:
            from urllib.parse import urljoin
            return urljoin(base_url, href)
        if base_url:
            from urllib.parse import urljoin
            return urljoin(base_url + "/", href)
        return href
