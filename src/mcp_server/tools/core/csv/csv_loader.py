"""Shared CSV loader with in-memory cache.

Used by the three core CSV tools (`csv_describe`, `csv_query`, `csv_soc`) so a
given file is only fetched from MinIO and parsed once per `(org_id, file_id)`
within the cache TTL.

Public API:
    - ``load_csv(tool, agent_context, file_id, **opts)`` — return a parsed
      ``polars.DataFrame`` plus a metadata dict (encoding, delimiter,
      truncated flag, …).
    - ``result_to_payload(df, max_inline_bytes=...)`` — serialise a Polars
      frame to a JSON-friendly preview, signalling whether it was truncated.

The loader does **not** open files from disk: the only ingestion point is
``BaseTool._load_file`` (MinIO + access control).
"""

from __future__ import annotations

import asyncio
import csv as _csv
import io
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import polars as pl

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.mcp_server.tools.base import BaseTool
    from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Tunables ───────────────────────────────────────────────────────────────
# Maximum bytes pulled from MinIO per file (cap the read to keep memory bounded).
DEFAULT_MAX_BYTES: int = 25 * 1024 * 1024  # 25 MB
# Cache settings.
_CACHE_TTL_SECONDS: float = 10 * 60.0  # 10 minutes
_CACHE_MAX_ENTRIES: int = 5
# Sniffer candidate delimiters, ordered by likelihood.
_DELIMITER_CANDIDATES: tuple[str, ...] = (",", ";", "\t", "|")


@dataclass
class CachedFrame:
    """Cached parsed CSV plus metadata."""

    df: pl.DataFrame
    meta: dict
    cached_at: float


# Cache keyed by (org_id, file_id).  Both values are str.
_cache: dict[tuple[str, str], CachedFrame] = {}
_cache_locks: dict[tuple[str, str], asyncio.Lock] = {}
_global_lock = asyncio.Lock()


def _evict_expired(now: float) -> None:
    """Drop entries whose TTL has elapsed."""
    expired = [k for k, v in _cache.items() if (now - v.cached_at) > _CACHE_TTL_SECONDS]
    for k in expired:
        _cache.pop(k, None)
        _cache_locks.pop(k, None)


def _evict_to_capacity() -> None:
    """Trim oldest entries until the cache fits within ``_CACHE_MAX_ENTRIES``."""
    while len(_cache) > _CACHE_MAX_ENTRIES:
        oldest_key = min(_cache, key=lambda k: _cache[k].cached_at)
        _cache.pop(oldest_key, None)
        _cache_locks.pop(oldest_key, None)


async def _get_lock(key: tuple[str, str]) -> asyncio.Lock:
    """Return a per-key lock, creating it under a short global lock."""
    async with _global_lock:
        lock = _cache_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _cache_locks[key] = lock
        return lock


def invalidate_cache(org_id: str, file_id: str) -> None:
    """Drop a cached entry (used after explicit user action / tests)."""
    key = (str(org_id), str(file_id))
    _cache.pop(key, None)
    _cache_locks.pop(key, None)


# ── Encoding / delimiter detection ─────────────────────────────────────────


def _detect_encoding(data: bytes) -> str:
    """Best-effort encoding detection.  Falls back to ``utf-8``."""
    try:
        import chardet  # type: ignore[import-untyped]

        sample = data[:65_536]
        guess = chardet.detect(sample) or {}
        enc = guess.get("encoding") or "utf-8"
        # Normalise common aliases / make UTF-8 the default for ASCII.
        enc_lower = enc.lower()
        if enc_lower in {"ascii", "us-ascii"}:
            return "utf-8"
        return enc
    except Exception:  # pragma: no cover - chardet failure is non-fatal
        return "utf-8"


def _detect_delimiter(text_sample: str) -> str:
    """Detect the column delimiter using ``csv.Sniffer`` with a fallback.

    The fallback counts occurrences of the candidate set on the first
    non-empty line — useful when ``Sniffer`` rejects ambiguous samples.
    """
    candidates = "".join(_DELIMITER_CANDIDATES)
    try:
        dialect = _csv.Sniffer().sniff(text_sample, delimiters=candidates)
        if dialect.delimiter in _DELIMITER_CANDIDATES:
            return dialect.delimiter
    except Exception:
        pass

    # Fallback: pick the most frequent candidate on the first data line.
    first_line = next(
        (ln for ln in text_sample.splitlines() if ln.strip()), ""
    )
    if not first_line:
        return ","
    best = max(
        _DELIMITER_CANDIDATES,
        key=lambda c: first_line.count(c),
    )
    # If no candidate appears, default to comma.
    if first_line.count(best) == 0:
        return ","
    return best


# ── Parsing ────────────────────────────────────────────────────────────────


def _parse_csv_bytes(
    data: bytes,
    *,
    delimiter: Optional[str],
    encoding: Optional[str],
) -> tuple[pl.DataFrame, dict]:
    """Decode + parse CSV bytes into a Polars frame.

    Returns the frame and a metadata dict describing the detected options.
    """
    chosen_encoding = encoding or _detect_encoding(data)
    try:
        text = data.decode(chosen_encoding, errors="replace")
    except (LookupError, TypeError):
        chosen_encoding = "utf-8"
        text = data.decode("utf-8", errors="replace")

    # Sample the first ~64 KB for delimiter sniffing.
    sample = text[:65_536]
    chosen_delimiter = delimiter or _detect_delimiter(sample)

    try:
        df = pl.read_csv(
            io.StringIO(text),
            separator=chosen_delimiter,
            infer_schema_length=1000,
            try_parse_dates=False,
            ignore_errors=False,
            truncate_ragged_lines=True,
        )
    except Exception as exc:
        # Retry once with permissive options before giving up.
        logger.warning(
            "csv_loader: strict parse failed (%s); retrying with ignore_errors=True",
            exc,
        )
        df = pl.read_csv(
            io.StringIO(text),
            separator=chosen_delimiter,
            infer_schema_length=1000,
            try_parse_dates=False,
            ignore_errors=True,
            truncate_ragged_lines=True,
        )

    schema = {name: str(dtype) for name, dtype in df.schema.items()}
    meta = {
        "encoding": chosen_encoding,
        "delimiter": chosen_delimiter,
        "rows": int(df.height),
        "columns": int(df.width),
        "schema": schema,
    }
    return df, meta


# ── Public API ─────────────────────────────────────────────────────────────


async def load_csv(
    tool: "BaseTool",
    agent_context: "AgentContext",
    file_id: str,
    *,
    delimiter: Optional[str] = None,
    encoding: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[pl.DataFrame, dict]:
    """Load and parse a CSV stored as a ``GSageFile``.

    Caches the parsed frame for ``_CACHE_TTL_SECONDS`` so the same file is not
    re-parsed across multiple tool invocations.  The cache key is
    ``(org_id, file_id)``.

    Raises
    ------
    FileNotFoundError
        File does not exist or the caller has no access.
    ValueError
        File could not be decoded / parsed as CSV.
    """
    org_id = str(agent_context.org_id)
    key = (org_id, str(file_id))

    now = time.monotonic()
    _evict_expired(now)

    cached = _cache.get(key)
    if cached is not None:
        return cached.df, dict(cached.meta)

    lock = await _get_lock(key)
    async with lock:
        # Re-check after acquiring the lock — another coroutine may have loaded it.
        cached = _cache.get(key)
        if cached is not None:
            return cached.df, dict(cached.meta)

        file_meta = await tool._load_file(
            file_id=file_id,
            org_id=org_id,
            user_id=str(agent_context.user_id),
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            max_bytes=max_bytes,
        )
        if file_meta is None:
            raise FileNotFoundError(
                f"File '{file_id}' not found or access denied for org '{org_id}'."
            )

        raw: bytes = file_meta.get("data") or b""
        if not raw:
            raise ValueError(f"File '{file_id}' is empty.")

        # Polars parsing is CPU-bound; offload to a worker thread.
        df, parse_meta = await asyncio.to_thread(
            _parse_csv_bytes,
            raw,
            delimiter=delimiter,
            encoding=encoding,
        )

        meta = {
            "file_id": str(file_meta.get("file_id", file_id)),
            "filename": file_meta.get("filename"),
            "content_type": file_meta.get("content_type"),
            "size_bytes": int(file_meta.get("size_bytes", len(raw))),
            "truncated": bool(file_meta.get("truncated", False)),
            **parse_meta,
        }

        _cache[key] = CachedFrame(df=df, meta=meta, cached_at=time.monotonic())
        _evict_to_capacity()
        return df, dict(meta)


def result_to_payload(
    df: pl.DataFrame,
    *,
    max_rows: int = 500,
    max_inline_bytes: int = 50_000,
) -> dict:
    """Serialise a Polars frame into an inline JSON-friendly preview.

    The payload contains:
      - ``columns``: ordered column names
      - ``rows``: list of row dicts (up to ``max_rows``)
      - ``row_count``: full frame height
      - ``truncated_rows`` / ``truncated_bytes``: flags

    Numbers / NaN are coerced to JSON-safe values via ``default=str``.
    """
    height = int(df.height)
    width = int(df.width)

    sliced = df.head(max_rows) if height > max_rows else df
    rows = sliced.to_dicts()
    payload: dict[str, Any] = {
        "columns": list(df.columns),
        "rows": rows,
        "row_count": height,
        "column_count": width,
        "truncated_rows": height > max_rows,
        "truncated_bytes": False,
    }

    # If the JSON exceeds the budget, halve the rows until it fits or we run out.
    serialised = json.dumps(payload, ensure_ascii=False, default=str)
    while len(serialised.encode("utf-8")) > max_inline_bytes and len(payload["rows"]) > 1:
        payload["rows"] = payload["rows"][: max(1, len(payload["rows"]) // 2)]
        payload["truncated_rows"] = True
        payload["truncated_bytes"] = True
        serialised = json.dumps(payload, ensure_ascii=False, default=str)

    return payload
