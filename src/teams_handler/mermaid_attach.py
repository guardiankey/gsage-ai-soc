"""Teams channel — Mermaid → inline PNG filter.

Microsoft Teams cannot render ```mermaid fenced blocks natively. To preserve
the user experience the backend pre-processes outgoing agent text:

1. Extracts every ```mermaid …``` block (up to ``MAX_DIAGRAMS_PER_TURN``).
2. Renders each block to PNG via the shared ``run_mmdc`` helper.
3. Replaces the block in the text with either nothing (success — the image
   is attached inline) or a Markdown fallback note (render failure / size
   exceeded / over-limit).
4. Returns the cleaned text, the list of Bot Framework ``Attachment``s and
   any per-block warnings.

Attachments are inline ``data:image/png;base64,...`` URIs so we don't need a
public download endpoint or any extra hosting/security surface.

A small in-process LRU cache (keyed by SHA-256 of the diagram source) avoids
re-rendering diagrams that the agent repeats across turns within the same
process lifetime.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
from collections import OrderedDict
from typing import Any

from src.shared.config.settings import get_settings
from src.shared.services.mermaid_renderer import run_mmdc

log = logging.getLogger(__name__)


# Regex over the whole response. Matches a fenced block whose info-string is
# exactly ``mermaid`` (optionally followed by spaces). Supports CRLF.
_MERMAID_RE = re.compile(
    r"^```[ \t]*mermaid[ \t]*\r?\n(.*?)\r?\n```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

# Hard caps tuned for Microsoft Teams (≈4 MB per activity payload).
MAX_DIAGRAMS_PER_TURN = 8
MAX_PNG_BYTES = 3 * 1024 * 1024  # 3 MB raw → ≈4 MB after base64

# In-process LRU. Key: sha256 of diagram_text. Value: png bytes (or None on
# permanent failure to avoid re-running mmdc on the same broken input).
_CACHE_MAX = 30
_render_cache: "OrderedDict[str, bytes | None]" = OrderedDict()


def _cache_get(key: str) -> tuple[bool, bytes | None]:
    if key in _render_cache:
        # Refresh recency.
        value = _render_cache.pop(key)
        _render_cache[key] = value
        return True, value
    return False, None


def _cache_put(key: str, value: bytes | None) -> None:
    _render_cache[key] = value
    while len(_render_cache) > _CACHE_MAX:
        _render_cache.popitem(last=False)


async def _render_one(diagram_text: str, *, scale: int) -> bytes | None:
    """Render a single Mermaid block, with cache. Returns ``None`` on failure."""
    key = hashlib.sha256(
        f"{scale}:{diagram_text}".encode("utf-8")
    ).hexdigest()
    hit, cached = _cache_get(key)
    if hit:
        return cached

    settings = get_settings()
    try:
        stdout, stderr, rc, png = await run_mmdc(
            diagram_text=diagram_text,
            mmdc_bin=settings.mermaid_cli_bin,
            want_png=True,
            timeout=settings.mermaid_validate_timeout_seconds,
            scale=scale,
        )
    except asyncio.TimeoutError:
        log.warning("Mermaid render timed out (key=%s)", key[:12])
        _cache_put(key, None)
        return None
    except FileNotFoundError:
        log.error("mmdc binary not found at %r — Mermaid filter disabled",
                  settings.mermaid_cli_bin)
        return None
    except Exception:  # noqa: BLE001
        log.exception("Mermaid render crashed (key=%s)", key[:12])
        _cache_put(key, None)
        return None

    if rc != 0 or png is None:
        log.info(
            "Mermaid render failed rc=%s stderr=%s",
            rc, stderr[:200] if stderr else "",
        )
        _cache_put(key, None)
        return None

    _cache_put(key, png)
    return png


def _build_attachment(png: bytes, index: int) -> Any:
    """Build a Bot Framework inline-PNG ``Attachment``.

    Imported lazily so this module can be imported by tests without pulling
    the heavy ``botbuilder`` stack.
    """
    from botbuilder.schema import Attachment  # local import

    b64 = base64.b64encode(png).decode("ascii")
    return Attachment(
        content_type="image/png",
        content_url=f"data:image/png;base64,{b64}",
        name=f"diagram-{index}.png",
    )


async def extract_and_render_mermaid(
    text: str,
    *,
    scale: int = 2,
) -> tuple[str, list[Any], list[str]]:
    """Extract Mermaid blocks from ``text``, render and attach.

    Parameters
    ----------
    text:
        Raw agent response (Markdown).
    scale:
        ``mmdc --scale`` factor. Default ``2`` keeps Teams payloads under
        the 4 MB activity limit.

    Returns
    -------
    cleaned_text:
        ``text`` with every Mermaid block replaced (by nothing on success or
        a fallback note on failure).
    attachments:
        Ordered list of ``botbuilder.schema.Attachment`` objects to set on
        the outgoing activity.
    warnings:
        Free-form, per-block diagnostic strings (for logging only).
    """
    if not text or "```mermaid" not in text:
        return text, [], []

    matches = list(_MERMAID_RE.finditer(text))
    if not matches:
        return text, [], []

    attachments: list[Any] = []
    warnings: list[str] = []

    # Render in-order; cache makes duplicates cheap.
    pieces: list[str] = []
    cursor = 0
    diagram_index = 0
    for m in matches:
        pieces.append(text[cursor:m.start()])
        cursor = m.end()

        diagram_index += 1
        if diagram_index > MAX_DIAGRAMS_PER_TURN:
            pieces.append("*(diagrama omitido — limite excedido)*")
            warnings.append(
                f"diagram #{diagram_index} skipped (limit {MAX_DIAGRAMS_PER_TURN})"
            )
            continue

        diagram_text = m.group(1)
        png = await _render_one(diagram_text, scale=scale)

        if png is None:
            pieces.append("*(falha ao renderizar diagrama)*")
            warnings.append(f"diagram #{diagram_index} render failed")
            continue

        if len(png) > MAX_PNG_BYTES:
            pieces.append("*(diagrama muito grande — peça versão menor)*")
            warnings.append(
                f"diagram #{diagram_index} too large ({len(png)} bytes)"
            )
            continue

        try:
            attachments.append(_build_attachment(png, len(attachments) + 1))
        except Exception:  # noqa: BLE001
            log.exception("Failed to build attachment for diagram #%d",
                          diagram_index)
            pieces.append("*(falha ao anexar diagrama)*")
            warnings.append(f"diagram #{diagram_index} attach failed")

    pieces.append(text[cursor:])

    cleaned = "".join(pieces)
    # Collapse triple+ newlines that may appear after block removal.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    return cleaned, attachments, warnings
