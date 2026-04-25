"""gSage AI — Tool filtering helpers for the MCP registry.

Parses ``TOOLS_ENABLED`` / ``TOOLS_DISABLED`` CSV env settings and evaluates
whether a given tool (by name / category / module path) should be loaded.

Pattern syntax (per CSV entry)::

    <glob>                # matches tool.name (default)
    name:<glob>           # matches tool.name
    category:<glob>       # matches tool.category
    module:<glob>         # matches the python module path
    core:<true|false>     # matches tool.core_tool flag
    re:<regex>            # regex against tool.name

Globs follow ``fnmatch`` rules (``*``, ``?``, ``[abc]``).

Rules
-----
* Empty ``TOOLS_ENABLED`` → all tools are admitted (legacy behaviour).
* Non-empty ``TOOLS_ENABLED`` → tool must match **at least one** pattern.
* ``TOOLS_DISABLED`` is evaluated **after** the allow check and always
  wins (deny > allow).
* ``module:`` patterns can be evaluated **before** importing the module,
  which avoids pulling heavy dependencies for filtered packages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Iterable

# Fields supported by the <field>:<value> prefix syntax.
_FIELDS = ("name", "category", "module", "re", "core")


@dataclass(frozen=True)
class _Pattern:
    field: str      # "name" | "category" | "module" | "re"
    raw: str        # original pattern text (glob or regex)
    regex: re.Pattern[str] | None = None  # pre-compiled when field == "re"


def parse_patterns(csv: str) -> list[_Pattern]:
    """Parse a CSV list of patterns into :class:`_Pattern` entries.

    Invalid entries are skipped silently to keep startup robust —
    operators get immediate feedback via :func:`describe_patterns`.
    """
    if not csv:
        return []

    out: list[_Pattern] = []
    for raw_entry in csv.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue

        if ":" in entry:
            field, _, value = entry.partition(":")
            field = field.strip().lower()
            value = value.strip()
            if field not in _FIELDS or not value:
                # Not a recognised field prefix — treat the whole entry as
                # a name glob (preserves the colon as a literal character).
                out.append(_Pattern(field="name", raw=entry))
                continue
            if field == "re":
                try:
                    compiled = re.compile(value)
                except re.error:
                    # Invalid regex — skip silently, described elsewhere.
                    continue
                out.append(_Pattern(field="re", raw=value, regex=compiled))
            else:
                out.append(_Pattern(field=field, raw=value))
        else:
            out.append(_Pattern(field="name", raw=entry))

    return out


def describe_patterns(patterns: list[_Pattern]) -> list[str]:
    """Return human-readable pattern descriptions for startup logging."""
    return [
        f"{p.field}:{p.raw}" if p.field != "name" or ":" in p.raw else p.raw
        for p in patterns
    ]


# ── Matching ────────────────────────────────────────────────────────────


def _matches(
    patterns: Iterable[_Pattern],
    *,
    name: str | None,
    category: str | None,
    module: str | None,
    core: bool | None = None,
) -> bool:
    for p in patterns:
        value: str | None
        if p.field == "name":
            value = name
        elif p.field == "category":
            value = category
        elif p.field == "module":
            value = module
        elif p.field == "core":
            if core is None:
                continue
            truthy = p.raw.strip().lower() in ("1", "true", "yes", "on", "*")
            if bool(core) == truthy:
                return True
            continue
        elif p.field == "re":
            value = name
            if value is not None and p.regex is not None and p.regex.search(value):
                return True
            continue
        else:
            continue

        if value is None:
            continue
        if fnmatchcase(value, p.raw):
            return True
    return False


def module_allowed(
    module_name: str,
    enabled: list[_Pattern],
    disabled: list[_Pattern],
) -> bool:
    """Evaluate ``module:*`` patterns *before* importing the module.

    * Patterns targeting ``name``/``category``/``re`` are **ignored** here
      — they require the tool class to be available.  Those are re-checked
      by :func:`tool_allowed` once the module is imported.
    * If the deny list contains a ``module:`` glob that matches, refuse
      the import.
    * If the allow list contains *any* ``module:`` glob and **none** of
      them match, refuse the import.  Allow lists that contain no
      ``module:`` patterns do not filter module imports (we must import to
      inspect the class).
    """
    module_disabled = [p for p in disabled if p.field == "module"]
    if module_disabled and _matches(
        module_disabled, name=None, category=None, module=module_name
    ):
        return False

    module_enabled = [p for p in enabled if p.field == "module"]
    if module_enabled and not _matches(
        module_enabled, name=None, category=None, module=module_name
    ):
        return False

    return True


def tool_allowed(
    *,
    name: str,
    category: str | None,
    module: str | None,
    core: bool = False,
    enabled: list[_Pattern],
    disabled: list[_Pattern],
) -> bool:
    """Evaluate the full allow/deny ruleset against a tool class."""
    if disabled and _matches(
        disabled, name=name, category=category, module=module, core=core
    ):
        return False

    if enabled and not _matches(
        enabled, name=name, category=category, module=module, core=core
    ):
        return False

    return True
