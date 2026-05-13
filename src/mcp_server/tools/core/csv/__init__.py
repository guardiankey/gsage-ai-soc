"""Core CSV tools — describe, query, join, SOC enrichment, and editing over CSV files.

The five tools (`csv_describe`, `csv_query`, `csv_join`, `csv_soc`, `csv_edit`)
share an in-memory loader/cache (`csv_loader.py`) so the same CSV file is parsed
only once per `(org_id, file_id)` pair within the cache TTL.

Shared utilities (SQL sandbox, DuckDB helpers, filter builders, value-source
resolvers, sort-type detection) live in `csv_shared.py` and are imported by
all five tools.
"""
