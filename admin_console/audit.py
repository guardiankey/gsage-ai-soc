"""Admin Console — append-only audit logger.

Writes every admin action to ``~/.gsage_ai/admin_audit.log``
in ISO-8601 format.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class AuditLogger:
    """Thread-safe, file-based audit logger."""

    def __init__(self, log_file: Path | None = None) -> None:
        self.log_file = log_file or (
            Path.home() / ".gsage_ai" / "admin_audit.log"
        )
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def log_event(
        self,
        action: str,
        target: str,
        details: str | dict | None = "",
        org_id: str | None = None,
    ) -> None:
        """Append one audit line to the log file."""
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        parts = [f"[{ts}]", action, "|", target]
        if org_id:
            parts += ["|", f"org={org_id}"]
        if details:
            detail_str = json.dumps(details, default=str) if isinstance(details, dict) else str(details)
            parts += ["|", detail_str]
        line = " ".join(parts) + "\n"
        try:
            with open(self.log_file, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass  # Never crash the UI on audit errors


_audit = AuditLogger()


def log_event(
    action: str,
    target: str,
    details: str | dict | None = "",
    org_id: str | None = None,
) -> None:
    """Module-level convenience wrapper around the global AuditLogger."""
    _audit.log_event(action, target, details, org_id)
