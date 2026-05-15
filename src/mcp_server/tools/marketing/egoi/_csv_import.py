"""gSage AI — CSV → E-goi bulk-import helpers.

Given a Polars ``DataFrame`` produced by ``csv_loader.load_csv`` and a
user-supplied column→E-goi-field mapping, build the list of contact
payloads expected by ``POST /lists/{id}/contacts/actions/import-bulk``
and split it into chunks that respect the 20 MB request-body limit.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator, Optional

import polars as pl

from src.mcp_server.tools.marketing.egoi import _query as Q

log = logging.getLogger(__name__)


# Base contact fields recognised by E-goi (kept as plain top-level keys
# under ``base`` in the import payload). Anything else falls into
# ``extra`` keyed by field_id (which must be a numeric extra-field id
# configured on the E-goi list).
EGOI_BASE_FIELDS: frozenset[str] = frozenset(
    {
        "email",
        "first_name",
        "last_name",
        "cellphone",
        "telephone",
        "birth_date",
        "lang",
        "vat",
        "title",
        "fax",
        "address",
        "address_2",
        "zip",
        "city",
        "district",
        "country",
    }
)


class CSVImportError(Exception):
    """Raised when CSV→E-goi mapping fails."""


def _coerce_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v if v else None
    return value


def parse_csv_to_contacts(
    df: pl.DataFrame,
    *,
    email_column: str,
    field_mapping: dict[str, str],
) -> list[dict]:
    """Convert a Polars DataFrame into E-goi import-bulk contact dicts.

    Parameters
    ----------
    df :
        Already-loaded CSV as a Polars DataFrame.
    email_column :
        Name of the CSV column that holds the primary email address.
        Rows with empty/null email are skipped.
    field_mapping :
        ``{csv_column: egoi_field}`` mapping. ``egoi_field`` may be one
        of :data:`EGOI_BASE_FIELDS` (placed under ``base``) or a numeric
        extra-field id as a string (placed under ``extra``).
    """
    if email_column not in df.columns:
        raise CSVImportError(
            f"email_column '{email_column}' not present in CSV columns: {df.columns}"
        )
    # Ensure the email column is always part of the mapping under 'email'.
    mapping = dict(field_mapping or {})
    mapping.setdefault(email_column, "email")

    unknown = [c for c in mapping if c not in df.columns]
    if unknown:
        raise CSVImportError(f"Columns not present in CSV: {unknown}")

    contacts: list[dict] = []
    for row in df.iter_rows(named=True):
        email = _coerce_value(row.get(email_column))
        if not email:
            continue
        base: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for csv_col, egoi_field in mapping.items():
            value = _coerce_value(row.get(csv_col))
            if value is None:
                continue
            if egoi_field in EGOI_BASE_FIELDS:
                base[egoi_field] = value
            else:
                # extra field — must be a numeric id (string or int)
                try:
                    extra[str(int(egoi_field))] = value
                except (TypeError, ValueError):
                    raise CSVImportError(
                        f"Unknown E-goi field '{egoi_field}' for column '{csv_col}'. "
                        f"Use one of {sorted(EGOI_BASE_FIELDS)} or a numeric extra-field id."
                    )
        # 'email' must live under base for the API
        base.setdefault("email", email)
        entry: dict[str, Any] = {"base": base}
        if extra:
            entry["extra"] = extra
        contacts.append(entry)
    return contacts


def chunk_contacts(
    contacts: list[dict],
    *,
    mode: str = "upsert",
    compare_field: str = "email",
    force_empty: bool = False,
    notify: bool = False,
    max_size_bytes: int = Q.IMPORT_BULK_MAX_BYTES,
) -> Iterator[dict]:
    """Yield ``ImportBulkRequest`` payload dicts that stay under the size cap.

    Each yielded dict already follows the API shape::

        {"mode": "upsert", "compare_field": "email", "contacts": [...]}

    Splits ``contacts`` into the largest possible chunks whose serialised
    JSON length is below ``max_size_bytes``.
    """
    if not contacts:
        return
    template: dict[str, Any] = {
        "mode": mode,
        "compare_field": compare_field,
        "force_empty": force_empty,
        "notify": notify,
        "contacts": [],
    }
    # Approximate overhead of the wrapper (keys + commas + braces).
    wrapper_overhead = len(json.dumps(template).encode("utf-8")) + 16

    current: list[dict] = []
    current_size = wrapper_overhead
    for c in contacts:
        encoded = json.dumps(c, ensure_ascii=False).encode("utf-8")
        # +2 for the comma and surrounding whitespace between array items
        addition = len(encoded) + 2
        if current and current_size + addition > max_size_bytes:
            payload = dict(template)
            payload["contacts"] = current
            yield payload
            current = []
            current_size = wrapper_overhead
        current.append(c)
        current_size += addition
    if current:
        payload = dict(template)
        payload["contacts"] = current
        yield payload
