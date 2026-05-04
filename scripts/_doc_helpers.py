"""Shared helpers for the documentation generators (`generate_tools_docs.py`
and `generate_channels_docs.py`).

These were extracted verbatim from the original tools-docs generator to
avoid duplicating Markdown / JSON-Schema / env-var rendering logic across
the two scripts.

The helpers have **no dependency on any specific data model** — they
operate purely on dicts and JSON-Schema fragments. The two callers wrap
them around their own metadata structures (``ToolInfo`` /
``ChannelSpec``).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Optional


# ──────────────────────────────────────────────────────────────────────
# JSON-Schema helpers
# ──────────────────────────────────────────────────────────────────────


def schema_properties(schema: Optional[dict]) -> dict[str, dict]:
    """Return the JSON-Schema-style properties map (handles flat fallback)."""
    if not schema:
        return {}
    if "properties" in schema and isinstance(schema["properties"], dict):
        return schema["properties"]
    # Flat: top-level keys ARE the fields (legacy form). Skip JSON-Schema meta.
    meta = {"type", "required", "additionalProperties", "description", "title"}
    out: dict[str, dict] = {}
    for k, v in schema.items():
        if k in meta or not isinstance(v, dict):
            continue
        out[k] = v
    return out


def required_fields(schema: Optional[dict]) -> list[str]:
    if not schema:
        return []
    req = schema.get("required") if isinstance(schema, dict) else None
    return list(req) if isinstance(req, list) else []


def field_default(props: dict[str, dict], defaults: dict, fname: str) -> Any:
    """Return the most authoritative default for a field, if any."""
    schema_default = props.get(fname, {}).get("default")
    if schema_default is not None:
        return schema_default
    return defaults.get(fname)


def schema_type(field_info: dict) -> str:
    t = field_info.get("type")
    if isinstance(t, list):
        return " | ".join(str(x) for x in t)
    if isinstance(t, str):
        return t
    if "enum" in field_info:
        vals = field_info.get("enum") or []
        return f"enum({', '.join(repr(v) for v in vals)})"
    return "—"


def placeholder_for(finfo: dict) -> Any:
    """Return a JSON-friendly placeholder value matching a schema field type."""
    t = finfo.get("type")
    if t == "boolean":
        return False
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "array":
        return []
    if t == "object":
        return {}
    enum_vals = finfo.get("enum")
    if enum_vals:
        return enum_vals[0]
    return "<value>"


# ──────────────────────────────────────────────────────────────────────
# Markdown rendering primitives
# ──────────────────────────────────────────────────────────────────────


def md_escape_cell(value: str) -> str:
    """Escape pipes and newlines so Markdown table cells survive."""
    return value.replace("\n", " ").replace("|", "\\|").strip()


def md_bool(value: bool) -> str:
    return "✓" if value else "—"


def md_link(rel_path: str) -> str:
    return f"[{rel_path}]({rel_path})"


def first_paragraph(text: Optional[str]) -> str:
    if not text:
        return ""
    for chunk in text.strip().split("\n\n"):
        cleaned = chunk.strip()
        if cleaned:
            return cleaned
    return text.strip()


# ──────────────────────────────────────────────────────────────────────
# Reusable Markdown blocks (configtool skeleton / example / field table)
# ──────────────────────────────────────────────────────────────────────


def render_config_skeleton(
    props: dict[str, dict], required: list[str], defaults: dict
) -> str:
    """Minimal JSON skeleton with every required field.

    Sensitive fields are included with a placeholder value because gSage
    currently stores them in plaintext inside the JSONB column — the
    skeleton must therefore reflect what an operator will actually see in
    the database.
    """
    skeleton: dict[str, Any] = {}
    for fname in required:
        finfo = props.get(fname, {})
        default = field_default(props, defaults, fname)
        skeleton[fname] = default if default is not None else placeholder_for(finfo)
    if not skeleton:
        return "{}"
    return json.dumps(skeleton, indent=2, ensure_ascii=False)


def render_config_example(props: dict[str, dict], defaults: dict) -> str:
    """Example JSON with placeholders for every field (sensitive included)."""
    payload: dict[str, Any] = {}
    for fname in sorted(props.keys()):
        finfo = props[fname]
        default = field_default(props, defaults, fname)
        payload[fname] = default if default is not None else placeholder_for(finfo)
    if not payload:
        return "{}"
    return json.dumps(payload, indent=2, ensure_ascii=False)


def render_field_reference(
    props: dict[str, dict], required: list[str], defaults: dict
) -> str:
    rows = [
        "| Field | Type | Required | Sensitive | Default | Description |",
        "| --- | --- | :---: | :---: | --- | --- |",
    ]
    for fname in sorted(props.keys()):
        finfo = props[fname]
        default = field_default(props, defaults, fname)
        default_repr = (
            "—" if default is None else f"`{json.dumps(default, ensure_ascii=False)}`"
        )
        rows.append(
            "| `{f}` | `{t}` | {req} | {sens} | {default} | {desc} |".format(
                f=md_escape_cell(fname),
                t=md_escape_cell(schema_type(finfo)),
                req=md_bool(fname in required),
                sens=md_bool(bool(finfo.get("sensitive"))),
                default=md_escape_cell(default_repr),
                desc=md_escape_cell(finfo.get("description") or "—"),
            )
        )
    return "\n".join(rows)


# ──────────────────────────────────────────────────────────────────────
# Loose env-var scanner (AST-based)
# ──────────────────────────────────────────────────────────────────────


_OS_NAMES = {"os"}


def _string_const(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def scan_loose_env_vars(
    source_files: list[Path],
    *,
    skip_prefix: Optional[str] = None,
) -> list[tuple[str, Optional[str]]]:
    """Return a sorted, deduplicated list of ``(name, default)`` env reads.

    Picks up calls like ``os.environ.get("FOO", ...)``,
    ``os.getenv("FOO", ...)`` and subscripts ``os.environ["FOO"]``.

    ``skip_prefix`` (e.g. ``"TOOL_"``) hides variables that are already
    rendered by the caller's own canonical env table.
    """
    found: dict[str, Optional[str]] = {}
    for fpath in source_files:
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                target: Optional[str] = None
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Attribute)
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id in _OS_NAMES
                    and func.value.attr == "environ"
                    and func.attr == "get"
                ):
                    target = "os.environ.get"
                elif (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id in _OS_NAMES
                    and func.attr == "getenv"
                ):
                    target = "os.getenv"
                if target and node.args:
                    name = _string_const(node.args[0])
                    if not name:
                        continue
                    default_val: Optional[str] = None
                    if len(node.args) >= 2:
                        c = _string_const(node.args[1])
                        if c is not None:
                            default_val = c
                    if skip_prefix and name.startswith(skip_prefix) and "__" in name:
                        continue
                    found.setdefault(name, default_val)
            if isinstance(node, ast.Subscript):
                value = node.value
                if (
                    isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id in _OS_NAMES
                    and value.attr == "environ"
                ):
                    name = _string_const(node.slice)
                    if name and not (
                        skip_prefix
                        and name.startswith(skip_prefix)
                        and "__" in name
                    ):
                        found.setdefault(name, None)
    return sorted(found.items(), key=lambda kv: kv[0])


# ──────────────────────────────────────────────────────────────────────
# Misc: env value repr + idempotent file write
# ──────────────────────────────────────────────────────────────────────


def env_value_repr(value: Any) -> str:
    """Render a default value as a `.env`-friendly literal."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def write_if_changed(path: Path, content: str, *, check_only: bool) -> bool:
    """Return True if content differs from disk (and write unless check_only)."""
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    if existing == content:
        return False
    if not check_only:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return True
