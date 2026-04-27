"""Standalone batch document ingestion tool for gSage AI.

Usage
-----
./ingest-documents <file_or_folder> [options]

Or directly:
    python -m cli_client.ingest <file_or_folder> [options]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Allow running as `python3 ./cli_client/ingest.py` in addition to `python -m cli_client.ingest`
_PROJECT_ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ALLOWED_EXTENSIONS = {
    # documents
    ".pdf", ".docx", ".doc", ".txt", ".md", ".rst",
    ".xlsx", ".xls", ".pptx", ".ppt", ".csv",
    ".html", ".htm", ".json", ".xml", ".eml",
    # archives
    ".zip", ".tar", ".gz", ".tar.gz", ".tar.bz2", ".tar.xz",
}
_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB (archives may be large)


def _get_effective_ext(name: str) -> str:
    """Return the canonical extension, handling double suffixes like .tar.gz."""
    p = Path(name)
    suffixes = p.suffixes
    if len(suffixes) >= 2:
        double = "".join(suffixes[-2:]).lower()
        if double in {".tar.gz", ".tar.bz2", ".tar.xz"}:
            return double
    return p.suffix.lower()
_POLL_INTERVAL_S = 10
_TERMINAL_STATUSES = {"COMPLETED", "FAILED", "completed", "failed"}


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def _discover(paths: list[str], recursive: bool) -> list[Path]:
    """Expand files/folders to a deduplicated list of ingestible Paths."""
    found: list[Path] = []
    seen: set[Path] = set()

    for raw in paths:
        p = Path(raw).resolve()
        if p.is_file():
            candidates = [p]
        elif p.is_dir():
            if recursive:
                candidates = list(p.rglob("*"))
            else:
                candidates = list(p.glob("*"))
        else:
            logger.warning("Path not found, skipping: %s", raw)
            continue

        for candidate in sorted(candidates):
            if not candidate.is_file():
                continue
            if _get_effective_ext(candidate.name) not in _ALLOWED_EXTENSIONS:
                continue
            if candidate.stat().st_size == 0:
                logger.warning("Skipping empty file: %s", candidate)
                continue
            if candidate.stat().st_size > _MAX_FILE_BYTES:
                logger.warning(
                    "Skipping file exceeding %d MB limit: %s (%d bytes)",
                    _MAX_FILE_BYTES // (1024 * 1024),
                    candidate,
                    candidate.stat().st_size,
                )
                continue
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)

    return found


# ---------------------------------------------------------------------------
# Upload worker (runs in thread pool)
# ---------------------------------------------------------------------------


def _upload(
    path: Path,
    client: Any,
    scope: str,
) -> dict[str, Any]:
    """Upload a single file and return the raw IngestJobSubmitResponse dict.

    Raises on API error so the caller can catch per-file.
    """
    return client.ingest_document(filepath=str(path), scope=scope)


# ---------------------------------------------------------------------------
# Status polling
# ---------------------------------------------------------------------------


def _poll_jobs(
    jobs: list[dict[str, Any]],
    client: Any,
    timeout: int,
) -> dict[str, dict[str, Any]]:
    """Poll all job statuses until all are terminal or timeout is reached.

    Returns a mapping of job_id → final status dict.
    """
    pending: dict[str, dict[str, Any]] = {j["job_id"]: j for j in jobs}
    final: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + timeout

    while pending and time.monotonic() < deadline:
        for job_id in list(pending.keys()):
            try:
                status = client.get_ingest_status(job_id)
            except Exception as exc:
                logger.debug("Status poll error for %s: %s", job_id, exc)
                continue

            if status.get("status") in _TERMINAL_STATUSES:
                final[job_id] = status
                del pending[job_id]

        if pending:
            time.sleep(_POLL_INTERVAL_S)

    # Jobs still pending after timeout
    for job_id, job in pending.items():
        job["status"] = "TIMEOUT"
        final[job_id] = job

    return final


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingest-documents",
        description="Batch upload documents to the gSage knowledge base.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Upload a single file
  ./ingest-documents report.pdf

  # Upload all supported files in a folder
  ./ingest-documents ./docs/

  # Recurse into sub-folders with user scope
  ./ingest-documents ./policies/ --recursive --scope user

  # Upload 3 files in parallel, wait up to 5 minutes
  ./ingest-documents ./data/ -r --parallel 3 --wait 300

Auth (env vars, checked in order):
  GSAGE_API_KEY      - API key (preferred)
  GSAGE_EMAIL + GSAGE_PASSWORD  - email/password login
  GSAGE_ORG_ID       - required when using API key
  GSAGE_API_HOST     - API base URL (default: http://localhost:8000)
""",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="FILE_OR_FOLDER",
        help="Files or directories to ingest. Directories are scanned for supported files.",
    )
    parser.add_argument(
        "-r", "--recursive",
        action="store_true",
        default=False,
        help="Recurse into sub-directories when a folder is given.",
    )
    parser.add_argument(
        "--scope",
        choices=["org", "user"],
        default="org",
        help="Ingest scope: 'org' (shared, default) or 'user' (personal).",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        metavar="N",
        help="Number of concurrent uploads (1–8, default: 1).",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        default=False,
        help="Do not poll job status after upload. Exit immediately with job IDs.",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=120,
        metavar="SECONDS",
        help="Max seconds to wait for ingestion to complete (default: 120).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Discover files and print them without uploading.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose/debug logging.",
    )
    return parser


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _build_client() -> Any:
    """Build a GSageAPIClient from environment variables.

    Priority: GSAGE_API_KEY > GSAGE_EMAIL/GSAGE_PASSWORD.
    Raises SystemExit(1) if configuration is missing.
    """
    from cli_client.client import GSageAPIClient, NotAuthenticatedError  # noqa: PLC0415
    from cli_client.config import Config, _resolve_default_api_host  # noqa: PLC0415

    api_key = os.environ.get("GSAGE_API_KEY", "").strip()
    email = os.environ.get("GSAGE_EMAIL", "").strip()
    password = os.environ.get("GSAGE_PASSWORD", "").strip()
    org_id = os.environ.get("GSAGE_ORG_ID", "").strip()
    api_host = _resolve_default_api_host()

    if not api_key and not (email and password):
        print(
            "ERROR: Authentication required.\n"
            "Set GSAGE_API_KEY or both GSAGE_EMAIL + GSAGE_PASSWORD.",
            file=sys.stderr,
        )
        sys.exit(1)

    config = Config(
        api_host=api_host,
        api_key=api_key or None,
        org_id=org_id or None,
        debug=False,
    )
    client = GSageAPIClient(config)

    if not api_key:
        try:
            client.login(email=email, password=password)
        except NotAuthenticatedError as exc:
            print(f"ERROR: Login failed: {exc}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            print(f"ERROR: Login failed: {exc}", file=sys.stderr)
            sys.exit(1)

    if not client.org_id:
        print(
            "ERROR: org_id could not be determined.\n"
            "Set GSAGE_ORG_ID or log in with email/password.",
            file=sys.stderr,
        )
        sys.exit(1)

    return client


def main() -> int:
    parser = _build_argparser()
    args = parser.parse_args()

    _setup_logging(args.verbose)

    # Clamp parallel workers
    parallel = max(1, min(args.parallel, 8))

    # Discover files
    files = _discover(args.paths, recursive=args.recursive)

    if not files:
        print("No supported files found. Supported formats: " + ", ".join(sorted(_ALLOWED_EXTENSIONS)))
        return 0

    print(f"Found {len(files)} file(s) to ingest.")

    if args.dry_run:
        for f in files:
            print(f"  {f}")
        return 0

    # Build client
    client = _build_client()

    # Upload phase
    submitted: list[dict[str, Any]] = []
    failed_upload: list[tuple[Path, str]] = []

    print(f"\nUploading {len(files)} file(s) with {parallel} parallel worker(s)...")

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        future_to_path = {
            pool.submit(_upload, path, client, args.scope): path
            for path in files
        }
        for future in as_completed(future_to_path):
            path = future_to_path[future]
            try:
                result = future.result()
                job_id = result.get("job_id", "?")
                print(f"  [QUEUED] {path.name}  job_id={job_id}")
                submitted.append(result)
            except Exception as exc:
                error_str = str(exc)
                print(f"  [FAILED] {path.name}  error={error_str}", file=sys.stderr)
                failed_upload.append((path, error_str))

    if not submitted:
        print("\nAll uploads failed.", file=sys.stderr)
        return 2

    print(f"\nQueued {len(submitted)} job(s). Failed to upload: {len(failed_upload)}.")

    if args.no_wait:
        print("\nJob IDs (use 'knowledge status <id>' to check progress):")
        for job in submitted:
            print(f"  {job['job_id']}  {job.get('filename', '?')}")
        return 0 if not failed_upload else 2

    # Polling phase
    print(f"\nWaiting up to {args.wait}s for ingestion to complete...")

    final = _poll_jobs(submitted, client, timeout=args.wait)

    # Summary
    completed = [j for j in final.values() if j.get("status") in ("COMPLETED", "completed")]
    failed_ingest = [j for j in final.values() if j.get("status") in ("FAILED", "failed")]
    timed_out = [j for j in final.values() if j.get("status") == "TIMEOUT"]

    print(f"\n{'=' * 50}")
    print(f"  Completed : {len(completed)}")
    print(f"  Failed    : {len(failed_ingest)}")
    print(f"  Timed out : {len(timed_out)}")
    print(f"  Upload err: {len(failed_upload)}")
    print(f"{'=' * 50}")

    if completed:
        total_chunks = sum(j.get("chunks_stored") or 0 for j in completed)
        print(f"\n  Total chunks stored: {total_chunks}")

    for job in failed_ingest:
        err = job.get("error_message") or "unknown error"
        print(f"  [FAILED] {job.get('filename', job.get('job_id'))}  {err}", file=sys.stderr)

    for job in timed_out:
        print(
            f"  [TIMEOUT] {job.get('filename', job.get('job_id'))}  "
            f"still processing — job_id={job.get('job_id')}",
            file=sys.stderr,
        )

    if failed_upload:
        for path, err in failed_upload:
            print(f"  [UPLOAD_ERROR] {path.name}  {err}", file=sys.stderr)

    all_ok = not failed_ingest and not timed_out and not failed_upload
    return 0 if all_ok else 2


if __name__ == "__main__":
    sys.exit(main())
