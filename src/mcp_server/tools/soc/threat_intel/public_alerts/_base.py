"""gSage AI — Base SourceParser for public security alert sources.

Each source (CTIR, CISC, CAIS, …) subclasses ``SourceParser`` and
implements ``parse_list()`` to extract alert items from the listing HTML.
"""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from typing import ClassVar, Optional

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

            # Clean summary: collapse whitespace, strip wrapping newlines
            raw_summary = item.get("summary")
            summary = cls._clean_summary(raw_summary) if raw_summary else None

            title = item.get("title", "")
            # Infer severity and categories from title + summary when
            # the parser didn't set them explicitly.
            severity = item.get("severity") or cls._infer_severity(title, summary)
            categories = item.get("categories") or cls._extract_categories(title, summary)

            alert = {
                "id": cls._make_id(item.get("title", ""), pub_date),
                "source": cls.source_id,
                "title": title,
                "content_url": item.get("url"),
                "severity": severity,
                "published_at": pub_date.isoformat(),
                "summary": summary,
                "categories": categories,
                "tlp": "TLP:WHITE",
            }
            alerts.append(alert)

        alerts.sort(key=lambda a: a["published_at"], reverse=True)
        return alerts

    # ── Summary / severity / categories helpers ───────────────────────────

    @staticmethod
    def _clean_summary(text: str) -> str:
        """Collapse whitespace and strip HTML artifact newlines."""
        import re
        # Collapse runs of whitespace (including \n) to single space
        cleaned = re.sub(r"\s+", " ", text)
        return cleaned.strip()[:500]

    _SEVERITY_PATTERNS: ClassVar[list[tuple[str, str]]] = [
        ("crítica|crítico|críticas|urgente|emergencial|exploração ativa|zero.day|0-day|rce|remoto.*execução", "high"),
        ("vulnerabilidade|exploração|comprometimento|ransomware|malware|backdoor|trojan|ameaça|incidente|ataque|invasão|breach|rootkit", "medium"),
        ("atualização|update|patch|boletim|conscientização|boas.práticas|recomendação|orientação|divulgação|informativo", "low"),
    ]

    _CATEGORY_PATTERNS: ClassVar[list[tuple[str, str]]] = [
        ("phishing|engenharia.social|fraude|spoofing", "phishing"),
        ("cve-|vulnerabilidade|patch|exploit|zero.day|0-day|rce|buffer.overflow|xss|sqli|injeção", "vulnerability"),
        ("ransomware|malware|backdoor|trojan|rootkit|wannacry|lockbit|blackcat|alphv", "malware"),
        ("vazamento|breach|exposição|dados.expostos|data.leak|informação.pessoal|lgpd", "data-leak"),
        ("cobalt.strike|c2|command.and.control|pivoting|lateral.movement|red.team", "apt"),
        ("ddos|negação.de.serviço|botnet|amplificação|reflection", "ddos"),
        ("cadeia.de.suprimentos|supply.chain|fornecedor|terceiro|software.update.comprometido", "supply-chain"),
        ("credencial|senha|autenticação|mfa|2fa|identity|identidade|logon|acesso", "credential-access"),
    ]

    @classmethod
    def _infer_severity(cls, title: str, summary: str | None) -> Optional[str]:
        """Infer severity from keywords in title + summary."""
        text = f"{title} {summary or ''}".lower()
        for pattern, level in cls._SEVERITY_PATTERNS:
            if re.search(pattern, text):
                return level
        return None

    @classmethod
    def _extract_categories(cls, title: str, summary: str | None) -> list[str]:
        """Extract categories from keywords in title + summary."""
        text = f"{title} {summary or ''}".lower()
        cats: list[str] = []
        for pattern, cat in cls._CATEGORY_PATTERNS:
            if re.search(pattern, text):
                cats.append(cat)
        return cats

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
