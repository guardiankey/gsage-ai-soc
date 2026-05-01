"""Core CSV tools — describe, query, and SOC enrichment over CSV files.

The three tools (`csv_describe`, `csv_query`, `csv_soc`) share an in-memory
loader/cache (`csv_loader.py`) so the same CSV file is parsed only once per
`(org_id, file_id)` pair within the cache TTL.
"""
