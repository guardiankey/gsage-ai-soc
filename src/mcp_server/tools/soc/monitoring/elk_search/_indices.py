"""gSage AI — Index pattern helpers for elk_search.

Responsible for:

* **Deny-list** — hard-coded patterns that must NEVER be queried on the
  external cluster.  Protects against accidentally pointing elk_search
  at the internal gSage cluster.
* **Allow-list** — per-profile explicit patterns (globs).  Empty
  allow-list = deny-all.
* **Collapse** — reduce a list of concrete index names to a compact list
  of glob patterns suitable for user display
  (e.g. ``logstash-2026.04.22`` → ``logstash-*``).
"""

from __future__ import annotations

import fnmatch
import re

# Hard-coded deny-list: internal gSage audit / trace / housekeeping
# indices.  These MUST NOT be exposed via elk_search, even when the
# admin accidentally points the profile at the internal cluster.
DENY_PATTERNS: tuple[str, ...] = (
    "gsage_*",
    "gsage-*",
    ".security-*",
    ".security",
    ".kibana*",
    ".apm-*",
    ".fleet-*",
    ".internal-*",
    ".ml-*",
    ".monitoring-*",
    ".watcher-*",
    ".async-search-*",
    ".tasks*",
    ".geoip_databases",
)

# Common date/sequence suffixes emitted by Logstash, Filebeat, Winlogbeat,
# rollover policies, etc.  Order matters — more specific first.
_SUFFIX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-\d{4}\.\d{2}\.\d{2}$"),   # -YYYY.MM.DD
    re.compile(r"-\d{4}-\d{2}-\d{2}$"),     # -YYYY-MM-DD
    re.compile(r"-\d{4}\.\d{2}$"),          # -YYYY.MM
    re.compile(r"-\d{4}-\d{2}$"),           # -YYYY-MM
    re.compile(r"-\d{4}\.w\d{2}$"),         # -YYYY.wNN
    re.compile(r"-\d{4}\.\d{2}\.\d{2}-\d+$"),  # rollover -YYYY.MM.DD-000001
    re.compile(r"-\d{6,}$"),                # long digit sequence
    re.compile(r"-\d+$"),                   # trailing digits (rollover)
)


def is_denied(name: str) -> bool:
    """Return ``True`` if *name* matches any hard-coded deny pattern."""
    lower = name.lower()
    return any(fnmatch.fnmatchcase(lower, pat.lower()) for pat in DENY_PATTERNS)


def match_allowed(name: str, allow_list: list[str] | tuple[str, ...]) -> bool:
    """Return ``True`` if *name* matches any pattern in *allow_list*.

    An empty *allow_list* means **deny-all** (returns ``False``).
    """
    if not allow_list:
        return False
    return any(fnmatch.fnmatchcase(name, pat) for pat in allow_list)


def filter_indices(
    names: list[str],
    allow_list: list[str] | tuple[str, ...],
) -> list[str]:
    """Return the subset of *names* that is both allowed and not denied."""
    return [
        n for n in names
        if not is_denied(n) and match_allowed(n, allow_list)
    ]


def collapse_index_patterns(names: list[str]) -> list[str]:
    """Collapse concrete index names into a de-duplicated list of glob patterns.

    Examples
    --------
    >>> collapse_index_patterns([
    ...     "logstash-2026.04.22",
    ...     "logstash-2026.04.23",
    ...     "filebeat-8.11.0-2026.04.22",
    ...     "winlogbeat-2026.04.22",
    ... ])
    ['filebeat-8.11.0-*', 'logstash-*', 'winlogbeat-*']
    """
    patterns: set[str] = set()
    for name in names:
        patterns.add(_collapse_one(name))
    return sorted(patterns)


def _collapse_one(name: str) -> str:
    for pat in _SUFFIX_PATTERNS:
        stripped = pat.sub("", name)
        if stripped != name:
            return f"{stripped}-*"
    return name
