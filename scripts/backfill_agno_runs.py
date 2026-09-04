#!/usr/bin/env python3
"""Restore agno 2.x chat history after an agno 3.x downgrade.

While the stack ran agno 3.x, the v3.0.0 storage migration moved session
runs out of ``ai.agno_sessions.runs`` into the ``ai.agno_runs`` table
(one row per run, payload in ``run_data`` JSONB, ordered by
``run_index, created_at``).  agno 2.x reads runs ONLY from the legacy
``runs`` column, so conversations whose runs live in ``agno_runs`` show up
empty in the web client.

This script merges the runs back into the legacy column:

- the legacy ``runs`` content keeps its stored order;
- on a ``run_id`` conflict the newest copy is kept (more messages wins;
  on a tie the most final status wins; on a tie the table copy is kept);
- runs that only exist in ``agno_runs`` (created during the 3.x window)
  are interleaved with the legacy runs by ``created_at``;
- the merged list is re-sorted by ``created_at`` so post-downgrade runs
  and 3.x-window runs end up in chronological order.

The ``agno_runs`` table is left in place — it is ignored by agno 2.x and
kept as a safety backup.

Usage (inside the backend container or with the project venv active):

    docker compose exec backend python scripts/backfill_agno_runs.py --dry-run
    docker compose exec backend python scripts/backfill_agno_runs.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional

# Add project root to Python path — the script ships at /app/scripts inside
# the container image, while the app source lives at /app/src.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.shared.config.settings import get_settings
from src.shared.database import create_pooled_engine

log = logging.getLogger(__name__)

SESSIONS_TABLE = "agno_sessions"
RUNS_TABLE = "agno_runs"

# Finality order used to break ties between two conflicting copies of the
# same run.  Terminal statuses are "more final" than paused/running.
_STATUS_FINALITY = {
    "running": 0,
    "paused": 1,
    "cancelled": 2,
    "completed": 3,
    "error": 3,
}


def _decode_json(value: Any) -> Any:
    """Decode a JSON/JSONB value read through a raw SELECT.

    asyncpg returns JSONB columns as ``str``; ORM round-trips return the
    decoded object.  Handles double-encoded legacy blobs defensively.
    Returns the decoded dict/list, or ``None`` when the value is not
    valid JSON.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    for _ in range(2):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return None
        else:
            break
    if isinstance(value, (list, dict)):
        return value
    return None


def _pick_conflicting_copy(
    run_id: str, legacy_run: dict, table_run: dict
) -> tuple[dict, str]:
    """Choose the most up-to-date copy of a run that exists in both places.

    The legacy column is frozen at migration time, but agno 2.x rewrites it
    (with in-place updates) for runs continued after the downgrade.  The
    runs table is updated by agno 3.x during the 3.x window.  Neither side
    is unconditionally newer, so pick by content:

    - identical dicts -> either (table copy);
    - more messages -> that copy won;
    - tie -> more final status wins (e.g. ``completed`` beats ``paused``);
    - final tie -> table copy.
    """
    if legacy_run == table_run:
        return table_run, "identical"

    legacy_messages = legacy_run.get("messages") or []
    table_messages = table_run.get("messages") or []
    if len(table_messages) > len(legacy_messages):
        return table_run, "table_more_messages"
    if len(legacy_messages) > len(table_messages):
        return legacy_run, "legacy_more_messages"

    legacy_status = str(legacy_run.get("status") or "")
    table_status = str(table_run.get("status") or "")
    if _STATUS_FINALITY.get(table_status, 0) > _STATUS_FINALITY.get(legacy_status, 0):
        return table_run, "table_status"
    return legacy_run, "legacy_default"


def merge_runs(legacy: Optional[list], table_runs: list) -> tuple[list, dict]:
    """Merge legacy-column runs with runs-table rows by ``run_id``."""
    stats = {
        "identical": 0,
        "table_more_messages": 0,
        "legacy_more_messages": 0,
        "table_status": 0,
        "legacy_default": 0,
        "legacy_only": 0,
        "table_only": 0,
        "skipped_malformed": 0,
    }

    table_by_id: dict[str, dict] = {}
    for run in table_runs:
        if isinstance(run, dict) and run.get("run_id"):
            table_by_id[run["run_id"]] = run

    merged: list[dict] = []
    seen: set[str] = set()

    for run in legacy or []:
        if not isinstance(run, dict):
            stats["skipped_malformed"] += 1
            continue
        rid = run.get("run_id")
        if rid is None:
            merged.append(run)
            continue
        table_copy = table_by_id.get(rid)
        if table_copy is None:
            merged.append(run)
            stats["legacy_only"] += 1
            seen.add(rid)
        else:
            chosen, how = _pick_conflicting_copy(rid, run, table_copy)
            merged.append(chosen)
            stats[how] += 1
            seen.add(rid)

    for run in table_runs:
        rid = run.get("run_id")
        if rid and rid not in seen:
            merged.append(run)
            seen.add(rid)
            stats["table_only"] += 1

    # Restore chronological order: runs that only existed on one side are
    # appended above and would otherwise sit out of order at the end.
    # Runs without a numeric created_at go last, keeping their relative
    # order (stable sort).
    def _sort_key(run: dict) -> tuple[int, Any]:
        created = run.get("created_at")
        if isinstance(created, (int, float)):
            return (0, created)
        return (1, 0)

    merged.sort(key=_sort_key)

    return merged, stats


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--schema",
        default="ai",
        help="Postgres schema holding the agno tables (default: ai)",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print a report without writing anything (default)",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="Write the merged runs back to agno_sessions",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Identifier whitelist: schema/table names are interpolated into SQL.
    schema = args.schema.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        log.error("Invalid schema name: %r", schema)
        return 2

    settings = get_settings()
    engine = create_pooled_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            rows = await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema "
                    "AND table_name = ANY(:names)"
                ),
                {"schema": schema, "names": [SESSIONS_TABLE, RUNS_TABLE]},
            )
            existing = {row[0] for row in rows.fetchall()}
            missing = {SESSIONS_TABLE, RUNS_TABLE} - existing
            if missing:
                log.warning(
                    "Missing table(s) in schema %r: %s — nothing to backfill",
                    schema, ", ".join(sorted(missing)),
                )
            if SESSIONS_TABLE not in existing:
                return 0

            result = await session.execute(
                text(
                    f'SELECT s.session_id, s.runs FROM "{schema}".{SESSIONS_TABLE} s '
                    f'WHERE EXISTS (SELECT 1 FROM "{schema}".{RUNS_TABLE} r '
                    f"WHERE r.session_id = s.session_id)"
                )
            )
            sessions = result.fetchall()

            if not sessions:
                log.info(
                    "No sessions with rows in %s.%s — nothing to backfill.",
                    schema, RUNS_TABLE,
                )
                return 0

            total_stats = {key: 0 for key in (
                "identical", "table_more_messages", "legacy_more_messages",
                "table_status", "legacy_default", "legacy_only", "table_only",
                "skipped_malformed",
            )}
            total_sessions = 0
            total_runs = 0
            changed_sessions = 0

            for sid, runs_raw in sessions:
                legacy = _decode_json(runs_raw)
                table_result = await session.execute(
                    text(
                        f'SELECT r.run_data FROM "{schema}".{RUNS_TABLE} r '
                        f"WHERE r.session_id = :sid "
                        f"ORDER BY r.run_index, r.created_at"
                    ),
                    {"sid": sid},
                )
                table_runs = [
                    run
                    for raw in table_result.fetchall()
                    if (run := _decode_json(raw[0])) is not None
                ]
                # run_data is a single run object per row; flatten defensively
                # in case a driver returned a list.
                flat_table_runs: list[dict] = []
                for item in table_runs:
                    if isinstance(item, list):
                        flat_table_runs.extend(
                            r for r in item if isinstance(r, dict)
                        )
                    elif isinstance(item, dict):
                        flat_table_runs.append(item)

                merged, stats = merge_runs(legacy, flat_table_runs)
                total_sessions += 1
                total_runs += len(merged)
                for key, value in stats.items():
                    total_stats[key] += value

                legacy_runs = legacy or []
                if merged != legacy_runs:
                    changed_sessions += 1
                    if args.apply:
                        await session.execute(
                            text(
                                f'UPDATE "{schema}".{SESSIONS_TABLE} '
                                f"SET runs = CAST(:runs AS JSONB) "
                                f"WHERE session_id = :sid"
                            ),
                            {
                                "runs": json.dumps(merged, ensure_ascii=False),
                                "sid": sid,
                            },
                        )

            if args.apply:
                await session.commit()
                mode = "APPLIED"
            else:
                mode = "DRY-RUN"

            log.info(
                "Backfill %s: %d session(s), %d total runs (%d session(s) changed)",
                mode, total_sessions, total_runs, changed_sessions,
            )
            log.info(
                "Conflict resolution: identical=%d table_more_messages=%d "
                "legacy_more_messages=%d table_status=%d legacy_default=%d",
                total_stats["identical"], total_stats["table_more_messages"],
                total_stats["legacy_more_messages"], total_stats["table_status"],
                total_stats["legacy_default"],
            )
            log.info(
                "One-sided runs: legacy_only=%d table_only=%d "
                "skipped_malformed=%d",
                total_stats["legacy_only"], total_stats["table_only"],
                total_stats["skipped_malformed"],
            )
            if not args.apply:
                log.info(
                    "Dry-run: no changes written. Re-run with --apply to persist."
                )
            else:
                log.info(
                    "The %s.%s table was left in place as a safety backup.",
                    schema, RUNS_TABLE,
                )
    finally:
        await engine.dispose()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
