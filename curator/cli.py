#!/usr/bin/env python3
"""Curator admin CLI.

A standalone, stdlib-only command-line tool to administer Curator reputation
lists from inside the Curator container (or any host that can reach the
service). It talks to the Curator admin HTTP API (``/a/*``) so that all writes
go through the same validation, upsert and differential-dump state machine the
service uses internally.

No third-party dependencies are required (argparse + urllib + csv + json).

Authentication
--------------
The admin API key (``X-API-Key`` header) is read from ``--api-key`` or the
``CURATOR_API_KEY`` environment variable (the same variable the service uses).
The base URL is read from ``--base-url`` or ``CURATOR_BASE_URL`` and defaults
to ``http://localhost:8000`` (the in-container bind address).

Examples
--------
    # List collections
    python3 /app/cli.py collections list --active-only

    # Add a single IOC (10-year expiry)
    python3 /app/cli.py item add proxy_lip 10.1.1.1 --type blocklist \\
        --expire 10y --ref "ticket #123"

    # Bulk import from a pipe-delimited file
    python3 /app/cli.py bulk import iocs.txt --dry-run

Bulk file format (pipe-delimited, one IOC per line)::

    slug|block_type|ioc|expiration|reference|public_reference

    - slug ............ collection slug or numeric collection id
    - block_type ...... blocklist | allowlist | suspected
    - ioc ............. the value (IP, domain, hash, email, ...)
    - expiration ...... 10y / 6m / 30d / 90 (days) / empty = never
    - reference ....... internal reference (optional, e.g. "ticket #123")
    - public_reference  public source reference (optional, e.g. CVE-2025-1)

Lines that are blank or start with ``#`` are ignored.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

# ── Constants (mirrors curator/app/models.py) ────────────────────────────────

ITEM_TYPES = ("blocklist", "allowlist", "suspected")
COLLECTION_TYPES = (
    "ip",
    "cidr",
    "domain",
    "url",
    "domain_regex",
    "file_hash_md5",
    "file_hash_sha1",
    "file_hash_sha256",
    "email",
    "asn",
    "ja3",
    "ja4",
)

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 30.0
BULK_MAX_ROWS = 10_000

# Multipliers used to translate a human expiry token into a number of days.
_EXPIRY_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}
_EXPIRY_NEVER = {"", "never", "none", "permanent", "perm", "0"}
_EXPIRY_RE = re.compile(r"^\s*(\d+)\s*([dwmy])\s*$", re.IGNORECASE)

# Shown in `--help` epilog. Concrete, copy-pasteable examples.
EXAMPLES = """\
Examples:
  # List collections (slug, type, item count, ...)
  cli.py collections list
  cli.py collections list --active-only --published-only

  # Create / update / delete a collection
  cli.py collection create --short-desc "Proxy IPs" --type ip --subtype lip
  cli.py collection update proxy_ips_lip_ip --no-published
  cli.py collection delete proxy_ips_lip_ip            # prompts for confirmation
  cli.py collection delete 7 --yes                     # by id, no prompt

  # Add / delete / list a single IOC (collection by slug or id)
  cli.py item add proxy_lip 10.1.1.1 --type blocklist --expire 10y --ref "ticket #123"
  cli.py item add proxy_lip evil.com --type blocklist --public-ref CVE-2025-0001
  cli.py item del proxy_lip 10.1.1.1 --type blocklist
  cli.py item list proxy_lip --type blocklist --per-page 100
  cli.py item list proxy_lip --expires-within-days 7

  # Bulk import — file format (one IOC per line):
  #   slug|block_type|ioc|expiration|reference|public_reference
  # expiration: 10y / 6m / 2w / 30d / N (days) / empty = never
  cli.py bulk import iocs.txt --dry-run        # validate without writing
  cli.py bulk import iocs.txt                  # from a file
  cat iocs.txt | cli.py bulk import -          # from stdin (pipe)
  cat iocs.txt | cli.py bulk import            # stdin (file arg omitted)

  # Example iocs.txt:
  #   # comment lines and blank lines are ignored
  #   proxy_lip|blocklist|10.1.1.1|10y|ticket #123
  #   proxy_lip|blocklist|10.1.1.2|6m|ticket #123|CVE-2025-0001
  #   mail_senders|allowlist|good@example.com||trusted partner

  # Authentication: --api-key / CURATOR_API_KEY, --base-url / CURATOR_BASE_URL.
  # Add --json to any command for machine-readable output.
"""



class CuratorError(Exception):
    """Raised for any fatal CLI / API error (mapped to a non-zero exit code)."""


# ── Expiry parsing ───────────────────────────────────────────────────────────


def parse_expiry(raw: Optional[str]) -> Optional[int]:
    """Translate a human expiry token into ``expire_days`` (int) or ``None``.

    Accepts ``10y`` / ``6m`` / ``2w`` / ``30d`` (unit-suffixed), a plain integer
    interpreted as days, or an "empty"/never token (``""``, ``never``, ``none``,
    ``permanent``, ``0``) which maps to ``None`` (no expiry).

    Raises :class:`CuratorError` for malformed tokens.
    """
    if raw is None:
        return None
    token = raw.strip().lower()
    if token in _EXPIRY_NEVER:
        return None

    match = _EXPIRY_RE.match(token)
    if match:
        amount = int(match.group(1))
        days = amount * _EXPIRY_UNIT_DAYS[match.group(2)]
        if days < 1:
            raise CuratorError(f"expiry must resolve to at least 1 day: {raw!r}")
        return days

    # Plain integer = number of days.
    if token.isdigit():
        days = int(token)
        if days < 1:
            raise CuratorError(f"expiry must resolve to at least 1 day: {raw!r}")
        return days

    raise CuratorError(
        f"invalid expiry {raw!r} (use e.g. 10y, 6m, 2w, 30d, a number of days, "
        "or 'never')"
    )


# ── HTTP client ──────────────────────────────────────────────────────────────


class CuratorClient:
    """Minimal synchronous HTTP client for the Curator admin API."""

    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._collections_cache: Optional[list[dict[str, Any]]] = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> Any:
        url = self.base_url + path
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url = f"{url}?{urllib.parse.urlencode(query)}"

        data: Optional[bytes] = None
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _extract_error_detail(exc)
            raise CuratorError(f"HTTP {exc.code} {method} {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CuratorError(
                f"cannot reach Curator at {self.base_url} ({exc.reason})"
            ) from exc

        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace")

    # ── Collections ──────────────────────────────────────────────────────────

    def list_collections(
        self, *, active_only: bool = False, published_only: bool = False
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/a/list_collections",
            params={
                "active_only": str(active_only).lower(),
                "published_only": str(published_only).lower(),
            },
        )

    def create_collection(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/a/create_collection", body=payload)

    def update_collection(
        self, collection_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "PUT", f"/a/{collection_id}/update_collection", body=payload
        )

    def del_collection(self, collection_id: int) -> dict[str, Any]:
        return self._request("DELETE", f"/a/{collection_id}/del_collection")

    # ── Items ──────────────────────────────────────────────────────────────────

    def add_item(self, collection_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/a/{collection_id}/add_item", body=payload)

    def del_item(self, collection_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("DELETE", f"/a/{collection_id}/del_item", body=payload)

    def view_items(
        self, collection_id: int, params: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request("GET", f"/a/{collection_id}/view_item", params=params)

    # ── Resolution helper ──────────────────────────────────────────────────────

    def resolve_collection_id(self, ref: str) -> int:
        """Resolve a collection reference (numeric id or slug) to an integer id."""
        ref = ref.strip()
        if ref.isdigit():
            return int(ref)

        if self._collections_cache is None:
            self._collections_cache = self.list_collections()

        matches = [c for c in self._collections_cache if c.get("slug") == ref]
        if not matches:
            available = ", ".join(sorted(c.get("slug", "") for c in self._collections_cache))
            raise CuratorError(
                f"collection slug {ref!r} not found. Available slugs: {available or '(none)'}"
            )
        return int(matches[0]["id"])


def _extract_error_detail(exc: urllib.error.HTTPError) -> str:
    """Pull a human-readable ``detail`` out of a Curator error response."""
    try:
        payload = json.loads(exc.read())
    except Exception:
        return exc.reason or "unknown error"
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])
    return str(payload)


# ── Output helpers ───────────────────────────────────────────────────────────


def _emit(data: Any, as_json: bool, text_renderer) -> None:
    """Print ``data`` as JSON or via a text renderer callable."""
    if as_json:
        print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
    else:
        text_renderer(data)


def _print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    """Render a simple aligned text table for the given columns."""
    if not rows:
        print("(no rows)")
        return
    widths = {c: len(c) for c in columns}
    for row in rows:
        for c in columns:
            widths[c] = max(widths[c], len(_cell(row.get(c))))
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("  ".join("-" * widths[c] for c in columns))
    for row in rows:
        print("  ".join(_cell(row.get(c)).ljust(widths[c]) for c in columns))


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


# ── Command handlers ─────────────────────────────────────────────────────────


def cmd_collections_list(client: CuratorClient, args: argparse.Namespace) -> int:
    collections = client.list_collections(
        active_only=args.active_only, published_only=args.published_only
    )

    def render(data: list[dict[str, Any]]) -> None:
        _print_table(
            data,
            ["id", "slug", "type", "subtype", "active", "published", "status", "item_count"],
        )
        print(f"\n{len(data)} collection(s).")

    _emit(collections, args.json, render)
    return 0


def cmd_collection_create(client: CuratorClient, args: argparse.Namespace) -> int:
    if args.type not in COLLECTION_TYPES:
        raise CuratorError(
            f"invalid --type {args.type!r}. Choose one of: {', '.join(COLLECTION_TYPES)}"
        )
    payload: dict[str, Any] = {
        "short_description": args.short_desc,
        "type": args.type,
        "active": args.active,
        "published": args.published,
    }
    if args.subtype is not None:
        payload["subtype"] = args.subtype
    if args.description is not None:
        payload["description"] = args.description

    result = client.create_collection(payload)

    def render(data: dict[str, Any]) -> None:
        print(f"Created collection id={data['id']} slug={data['slug']}")

    _emit(result, args.json, render)
    return 0


def cmd_collection_update(client: CuratorClient, args: argparse.Namespace) -> int:
    collection_id = client.resolve_collection_id(args.collection)
    payload: dict[str, Any] = {}
    if args.short_desc is not None:
        payload["short_description"] = args.short_desc
    if args.description is not None:
        payload["description"] = args.description
    if args.active is not None:
        payload["active"] = args.active
    if args.published is not None:
        payload["published"] = args.published

    if not payload:
        raise CuratorError(
            "nothing to update: supply at least one of --short-desc, --description, "
            "--active/--no-active, --published/--no-published"
        )

    result = client.update_collection(collection_id, payload)

    def render(data: dict[str, Any]) -> None:
        print(f"Updated collection id={data['id']} slug={data['slug']}")

    _emit(result, args.json, render)
    return 0


def cmd_collection_delete(client: CuratorClient, args: argparse.Namespace) -> int:
    collection_id = client.resolve_collection_id(args.collection)

    if not args.yes:
        # Show what will be removed and require explicit confirmation.
        try:
            collections = client.list_collections()
            match = next((c for c in collections if int(c["id"]) == collection_id), None)
        except CuratorError:
            match = None
        label = (
            f"{match['slug']} (id={collection_id}, {match.get('item_count', '?')} items)"
            if match
            else f"id={collection_id}"
        )
        prompt = (
            f"Permanently delete collection {label} and ALL its IOCs? "
            "This cannot be undone. Type 'yes' to confirm: "
        )
        try:
            answer = input(prompt)
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("Aborted.")
            return 1

    result = client.del_collection(collection_id)

    def render(data: dict[str, Any]) -> None:
        print(
            f"Deleted collection id={data['deleted_collection_id']} "
            f"slug={data['slug']} ({data['deleted_items']} item(s) removed)"
        )

    _emit(result, args.json, render)
    return 0


def cmd_item_add(client: CuratorClient, args: argparse.Namespace) -> int:
    if args.type not in ITEM_TYPES:
        raise CuratorError(
            f"invalid --type {args.type!r}. Choose one of: {', '.join(ITEM_TYPES)}"
        )
    expire_days = parse_expiry(args.expire)
    collection_id = client.resolve_collection_id(args.collection)

    payload: dict[str, Any] = {"value": args.value, "type": args.type}
    if args.public_ref is not None:
        payload["public_reference"] = args.public_ref
    if args.ref is not None:
        payload["reference"] = args.ref
    if expire_days is not None:
        payload["expire_days"] = expire_days

    result = client.add_item(collection_id, payload)

    def render(data: dict[str, Any]) -> None:
        shown = data.get("value") or data.get("cidr")
        expiry = data.get("expire_at") or "never"
        print(f"Added item id={data['id']} value={shown} type={data['type']} expires={expiry}")

    _emit(result, args.json, render)
    return 0


def cmd_item_del(client: CuratorClient, args: argparse.Namespace) -> int:
    if args.type not in ITEM_TYPES:
        raise CuratorError(
            f"invalid --type {args.type!r}. Choose one of: {', '.join(ITEM_TYPES)}"
        )
    collection_id = client.resolve_collection_id(args.collection)
    result = client.del_item(
        collection_id, {"value": args.value, "type": args.type}
    )

    def render(data: dict[str, Any]) -> None:
        print(f"Deleted {data.get('deleted', 0)} item(s) matching {data.get('value')!r}")

    _emit(result, args.json, render)
    return 0


def cmd_item_list(client: CuratorClient, args: argparse.Namespace) -> int:
    if args.type is not None and args.type not in ITEM_TYPES:
        raise CuratorError(
            f"invalid --type {args.type!r}. Choose one of: {', '.join(ITEM_TYPES)}"
        )
    collection_id = client.resolve_collection_id(args.collection)
    params: dict[str, Any] = {"page": args.page, "per_page": args.per_page}
    if args.value is not None:
        params["value"] = args.value
    if args.type is not None:
        params["type"] = args.type
    if args.within_days is not None:
        params["created_within_days"] = args.within_days
    if args.expires_within_days is not None:
        params["expires_within_days"] = args.expires_within_days
    if args.never_expires is not None:
        params["never_expires"] = str(args.never_expires).lower()
    if args.expired_only:
        params["expired_only"] = "true"

    result = client.view_items(collection_id, params)

    def render(data: dict[str, Any]) -> None:
        rows = data.get("items", [])
        display = [
            {
                "id": r.get("id"),
                "value": r.get("value") or r.get("cidr"),
                "type": r.get("type"),
                "created_at": r.get("created_at"),
                "expire_at": r.get("expire_at") or "never",
                "reference": r.get("reference"),
                "public_reference": r.get("public_reference"),
            }
            for r in rows
        ]
        _print_table(
            display,
            ["id", "value", "type", "created_at", "expire_at", "reference", "public_reference"],
        )
        print(
            f"\nPage {data.get('page')} — {len(rows)} of {data.get('total')} item(s)."
        )

    _emit(result, args.json, render)
    return 0


# ── Bulk import ──────────────────────────────────────────────────────────────


def _parse_bulk_row(fields: list[str], default_type: Optional[str]) -> dict[str, Any]:
    """Validate and normalise one parsed bulk row into an add_item payload spec.

    Returns a dict with ``slug``, ``type``, ``value``, ``expire_days``,
    ``reference``, ``public_reference``. Raises :class:`CuratorError` on any
    validation failure.
    """
    # Pad to 6 logical columns so trailing optionals can be omitted.
    cells = [c.strip() for c in fields]
    if len(cells) < 3:
        raise CuratorError(
            "expected at least 3 fields (slug|block_type|ioc), "
            f"got {len(cells)}"
        )
    while len(cells) < 6:
        cells.append("")

    slug, block_type, ioc, expiration, reference, public_reference = cells[:6]

    if not slug:
        raise CuratorError("missing collection slug/id")
    block_type = block_type or (default_type or "")
    if block_type not in ITEM_TYPES:
        raise CuratorError(
            f"invalid block_type {block_type!r} (expected one of {', '.join(ITEM_TYPES)})"
        )
    if not ioc:
        raise CuratorError("missing ioc value")

    expire_days = parse_expiry(expiration)

    spec: dict[str, Any] = {
        "slug": slug,
        "type": block_type,
        "value": ioc,
        "expire_days": expire_days,
        "reference": reference or None,
        "public_reference": public_reference or None,
    }
    return spec


def cmd_bulk_import(client: CuratorClient, args: argparse.Namespace) -> int:
    if args.default_type is not None and args.default_type not in ITEM_TYPES:
        raise CuratorError(
            f"invalid --default-type {args.default_type!r}. "
            f"Choose one of: {', '.join(ITEM_TYPES)}"
        )

    # Read from stdin when the path is "-" or omitted (piped input), else a file.
    source = args.file or "-"
    if source == "-":
        if sys.stdin.isatty():
            raise CuratorError(
                "no input on stdin (pipe a file or pass a path, e.g. "
                "'cat iocs.txt | cli.py bulk import -')"
            )
        raw_lines = sys.stdin.readlines()
    else:
        try:
            with open(source, "r", encoding="utf-8", newline="") as fh:
                raw_lines = fh.readlines()
        except OSError as exc:
            raise CuratorError(f"cannot read file {source!r}: {exc}") from exc

    # Parse + validate every row first (fail-soft, collecting per-row errors).
    specs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    line_no = 0
    for raw in raw_lines:
        line_no += 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(specs) + len(skipped) >= BULK_MAX_ROWS:
            raise CuratorError(
                f"bulk import exceeds the {BULK_MAX_ROWS} row hard cap"
            )
        # csv.reader handles quoting; delimiter configurable (default '|').
        fields = next(csv.reader([stripped], delimiter=args.delimiter))
        try:
            specs.append({"line": line_no, **_parse_bulk_row(fields, args.default_type)})
        except CuratorError as exc:
            skipped.append({"line": line_no, "reason": str(exc), "raw": stripped})

    # Total data rows considered (excludes comments/blank lines), captured
    # before slug resolution moves any specs into ``skipped``.
    rows_total = len(specs) + len(skipped)

    # Resolve slugs -> collection ids (cached) up-front, surfacing bad slugs.
    resolved: list[dict[str, Any]] = []
    for spec in specs:
        try:
            spec["collection_id"] = client.resolve_collection_id(spec["slug"])
            resolved.append(spec)
        except CuratorError as exc:
            skipped.append(
                {"line": spec["line"], "reason": str(exc), "raw": spec["value"]}
            )

    added = 0
    failed: list[dict[str, Any]] = []

    if not args.dry_run:
        for spec in resolved:
            payload: dict[str, Any] = {"value": spec["value"], "type": spec["type"]}
            if spec["reference"]:
                payload["reference"] = spec["reference"]
            if spec["public_reference"]:
                payload["public_reference"] = spec["public_reference"]
            if spec["expire_days"] is not None:
                payload["expire_days"] = spec["expire_days"]
            try:
                client.add_item(spec["collection_id"], payload)
                added += 1
            except CuratorError as exc:
                failed.append(
                    {"line": spec["line"], "value": spec["value"], "reason": str(exc)}
                )

    summary = {
        "action": "bulk_import",
        "source": "<stdin>" if source == "-" else source,
        "dry_run": args.dry_run,
        "rows_total": rows_total,
        "valid": len(resolved),
        "added": added,
        "failed": len(failed),
        "skipped": len(skipped),
        "errors": failed,
        "skipped_rows": skipped,
    }

    def render(data: dict[str, Any]) -> None:
        mode = "DRY-RUN (no writes)" if data["dry_run"] else "IMPORT"
        print(f"Bulk {mode}: {data['source']}")
        print(
            f"  rows={data['rows_total']} valid={data['valid']} "
            f"added={data['added']} failed={data['failed']} skipped={data['skipped']}"
        )
        for sk in data["skipped_rows"]:
            print(f"  [skip] line {sk['line']}: {sk['reason']}")
        for er in data["errors"]:
            print(f"  [fail] line {er['line']} ({er['value']}): {er['reason']}")

    _emit(summary, args.json, render)
    # Non-zero exit if anything failed or was skipped, so scripts can detect it.
    return 0 if not failed and not skipped else 1


# ── Argument parser ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Curator admin CLI — manage reputation lists via the admin API.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CURATOR_BASE_URL", DEFAULT_BASE_URL),
        help=f"Curator base URL (env CURATOR_BASE_URL; default {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("CURATOR_API_KEY"),
        help="Admin API key (env CURATOR_API_KEY).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"HTTP timeout in seconds (default {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text.",
    )

    sub = parser.add_subparsers(dest="group", required=True)

    # collections ---------------------------------------------------------------
    p_collections = sub.add_parser("collections", help="Collection-level read commands.")
    sub_collections = p_collections.add_subparsers(dest="action", required=True)
    p_col_list = sub_collections.add_parser("list", help="List collections.")
    p_col_list.add_argument("--active-only", action="store_true", help="Only active collections.")
    p_col_list.add_argument(
        "--published-only", action="store_true", help="Only published collections."
    )
    p_col_list.set_defaults(func=cmd_collections_list)

    # collection ----------------------------------------------------------------
    p_collection = sub.add_parser(
        "collection", help="Create, update or delete a collection."
    )
    sub_collection = p_collection.add_subparsers(dest="action", required=True)

    p_col_create = sub_collection.add_parser("create", help="Create a collection.")
    p_col_create.add_argument("--short-desc", required=True, help="Short description (max 100 chars).")
    p_col_create.add_argument(
        "--type", required=True, help=f"Collection type ({', '.join(COLLECTION_TYPES)})."
    )
    p_col_create.add_argument("--subtype", default=None, help="Optional subtype (max 20 chars).")
    p_col_create.add_argument("--description", default=None, help="Optional long description.")
    p_col_create.add_argument(
        "--active", action=argparse.BooleanOptionalAction, default=True, help="Collection active."
    )
    p_col_create.add_argument(
        "--published",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Collection published (public /data/ exposure).",
    )
    p_col_create.set_defaults(func=cmd_collection_create)

    p_col_update = sub_collection.add_parser("update", help="Update a collection.")
    p_col_update.add_argument("collection", help="Collection slug or numeric id.")
    p_col_update.add_argument("--short-desc", default=None, help="New short description.")
    p_col_update.add_argument("--description", default=None, help="New long description.")
    p_col_update.add_argument(
        "--active", action=argparse.BooleanOptionalAction, default=None, help="Set active flag."
    )
    p_col_update.add_argument(
        "--published",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Set published flag.",
    )
    p_col_update.set_defaults(func=cmd_collection_update)

    p_col_delete = sub_collection.add_parser(
        "delete",
        help="Delete a collection and ALL its IOCs (cascade, irreversible).",
        description=(
            "Permanently delete a collection, cascade-deleting every IOC it "
            "contains and removing its on-disk dump directory. Prompts for "
            "confirmation unless --yes is given."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_col_delete.add_argument("collection", help="Collection slug or numeric id.")
    p_col_delete.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt (for scripts).",
    )
    p_col_delete.set_defaults(func=cmd_collection_delete)

    # item ----------------------------------------------------------------------
    p_item = sub.add_parser("item", help="Add, delete or list IOCs.")
    sub_item = p_item.add_subparsers(dest="action", required=True)

    p_item_add = sub_item.add_parser("add", help="Add (upsert) a single IOC.")
    p_item_add.add_argument("collection", help="Collection slug or numeric id.")
    p_item_add.add_argument("value", help="IOC value (IP, domain, hash, ...).")
    p_item_add.add_argument(
        "--type", required=True, help=f"Block type ({', '.join(ITEM_TYPES)})."
    )
    p_item_add.add_argument("--public-ref", default=None, help="Public reference (e.g. CVE).")
    p_item_add.add_argument("--ref", default=None, help="Internal reference (e.g. ticket).")
    p_item_add.add_argument(
        "--expire", default=None, help="Expiry: 10y / 6m / 30d / N days / never."
    )
    p_item_add.set_defaults(func=cmd_item_add)

    p_item_del = sub_item.add_parser("del", help="Delete (soft) a single IOC.")
    p_item_del.add_argument("collection", help="Collection slug or numeric id.")
    p_item_del.add_argument("value", help="IOC value to delete.")
    p_item_del.add_argument(
        "--type", required=True, help=f"Block type ({', '.join(ITEM_TYPES)})."
    )
    p_item_del.set_defaults(func=cmd_item_del)

    p_item_list = sub_item.add_parser("list", help="List/filter IOCs in a collection.")
    p_item_list.add_argument("collection", help="Collection slug or numeric id.")
    p_item_list.add_argument("--value", default=None, help="Filter by exact value.")
    p_item_list.add_argument("--type", default=None, help=f"Filter by block type ({', '.join(ITEM_TYPES)}).")
    p_item_list.add_argument("--page", type=int, default=1, help="Page number (default 1).")
    p_item_list.add_argument(
        "--per-page", type=int, default=50, help="Items per page (default 50, max 500)."
    )
    p_item_list.add_argument(
        "--within-days", type=int, default=None, help="Only items created within the last N days."
    )
    p_item_list.add_argument(
        "--expires-within-days",
        type=int,
        default=None,
        help="Only items expiring within the next N days.",
    )
    p_item_list.add_argument(
        "--never-expires",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Only never-expiring items (--no-never-expires excludes them).",
    )
    p_item_list.add_argument(
        "--expired-only", action="store_true", help="Only already-expired items."
    )
    p_item_list.set_defaults(func=cmd_item_list)

    # bulk ----------------------------------------------------------------------
    p_bulk = sub.add_parser("bulk", help="Bulk operations from a file.")
    sub_bulk = p_bulk.add_subparsers(dest="action", required=True)
    p_bulk_import = sub_bulk.add_parser(
        "import",
        help="Bulk-import IOCs from a pipe-delimited file or stdin.",
        description=(
            "Bulk-import IOCs from a pipe-delimited source.\n\n"
            "File format (one IOC per line):\n"
            "  slug|block_type|ioc|expiration|reference|public_reference\n\n"
            "  slug ............ collection slug or numeric id\n"
            "  block_type ...... blocklist | allowlist | suspected\n"
            "  ioc ............. value (IP, domain, hash, email, ...)\n"
            "  expiration ...... 10y / 6m / 2w / 30d / N (days) / empty = never\n"
            "  reference ....... optional internal reference (e.g. ticket)\n"
            "  public_reference  optional public reference (e.g. CVE)\n\n"
            "Blank lines and lines starting with '#' are ignored.\n\n"
            "Input source:\n"
            "  cli.py bulk import iocs.txt        # from a file\n"
            "  cat iocs.txt | cli.py bulk import -  # from stdin\n"
            "  cat iocs.txt | cli.py bulk import    # stdin (arg omitted)\n\n"
            "Example:\n"
            "  proxy_lip|blocklist|10.1.1.1|10y|ticket #123\n"
            "  proxy_lip|blocklist|10.1.1.2|6m|ticket #123|CVE-2025-0001\n"
            "  mail_senders|allowlist|good@example.com||trusted partner"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_bulk_import.add_argument(
        "file",
        nargs="?",
        default="-",
        help="Path to the import file, or '-' for stdin (default: stdin).",
    )
    p_bulk_import.add_argument(
        "--delimiter", default="|", help="Field delimiter (default '|')."
    )
    p_bulk_import.add_argument(
        "--default-type",
        default=None,
        help=f"Fallback block type for rows missing it ({', '.join(ITEM_TYPES)}).",
    )
    p_bulk_import.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and resolve without writing anything.",
    )
    p_bulk_import.set_defaults(func=cmd_bulk_import)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.api_key:
        print(
            "error: missing API key (use --api-key or set CURATOR_API_KEY)",
            file=sys.stderr,
        )
        return 2

    client = CuratorClient(args.base_url, args.api_key, args.timeout)
    try:
        return args.func(client, args)
    except CuratorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
