#!/usr/bin/env python3
"""Generate TOOL_* and AUTH_* environment variable defaults in .env.example.

Run this script after adding or modifying tools or auth backends to keep
.env.example in sync with their configuration declarations.

The script rewrites two independent zones delimited by markers:

  TOOL DEFAULTS zone:
    # ── BEGIN TOOL DEFAULTS (auto-generated — do not edit manually) ──────────────
    ...auto-generated content...
    # ── END TOOL DEFAULTS (auto-generated) ──────────────────────────────────────

  AUTH PROVIDER DEFAULTS zone:
    # ── BEGIN AUTH PROVIDER DEFAULTS (auto-generated — do not edit manually) ─────
    ...auto-generated content...
    # ── END AUTH PROVIDER DEFAULTS (auto-generated) ──────────────────────────────

Usage:
    python scripts/generate_env_defaults.py [--dry-run]

Replaces: scripts/generate_tool_env_defaults.py
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import sys
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Project root on sys.path
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── TOOL zone markers (must match exactly what is in .env.example) ────────
TOOL_MARKER_BEGIN = (
    "# ── BEGIN TOOL DEFAULTS (auto-generated — do not edit manually) ──────────────"
)
TOOL_MARKER_END = (
    "# ── END TOOL DEFAULTS (auto-generated) ──────────────────────────────────────"
)

# ── AUTH PROVIDER zone markers ────────────────────────────────────────────
AUTH_MARKER_BEGIN = (
    "# ── BEGIN AUTH PROVIDER DEFAULTS (auto-generated — do not edit manually) ─────"
)
AUTH_MARKER_END = (
    "# ── END AUTH PROVIDER DEFAULTS (auto-generated) ──────────────────────────────"
)

# ── Packages to scan ─────────────────────────────────────────────────────
_TOOL_PACKAGES = [
    "src.mcp_server.tools",
    "custom_code.tools",
]

_AUTH_PACKAGES = [
    "src.shared.auth.backends",
    "custom_code.auth_backends",
]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SEPARATOR_WIDTH = 76


def _sep(label: str) -> str:
    right_dashes = max(2, _SEPARATOR_WIDTH - len(f"# ── {label} ") - 2)
    return f"# ── {label} {'─' * right_dashes}"


def _format_default(value: Any) -> str:
    """Render a Python default value as a suitable .env string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


# ---------------------------------------------------------------------------
# Tool discovery & block generation  (unchanged from generate_tool_env_defaults.py)
# ---------------------------------------------------------------------------

def _collect_subclasses(cls: type, result: dict[str, type]) -> None:
    """Recursively collect all concrete BaseTool subclasses."""
    name_attr = cls.__dict__.get("name") or getattr(cls, "name", None)
    if (
        not inspect.isabstract(cls)
        and isinstance(name_attr, str)
        and name_attr not in result
    ):
        result[name_attr] = cls
    for sub in cls.__subclasses__():
        _collect_subclasses(sub, result)


def discover_tools() -> dict[str, type]:
    """Import all tool packages and return {tool_name: class} mapping."""
    from src.mcp_server.tools.base import BaseTool  # noqa: F401

    for pkg_name in _TOOL_PACKAGES:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError as exc:
            print(f"  [skip] {pkg_name}: {exc}")
            continue

        pkg_paths = getattr(pkg, "__path__", None)
        if not pkg_paths:
            continue

        for _finder, mod_name, _ispkg in pkgutil.walk_packages(
            pkg_paths, prefix=f"{pkg_name}."
        ):
            try:
                importlib.import_module(mod_name)
            except Exception as exc:
                print(f"  [skip] {mod_name}: {exc}")

    from src.mcp_server.tools.base import BaseTool as _BaseTool

    tools: dict[str, type] = {}
    _collect_subclasses(_BaseTool, tools)
    return tools


def _get_all_fields(cls_obj: type) -> list[str]:
    """Return sorted list of all config field names for a tool or auth provider."""
    config_schema: Optional[dict] = getattr(cls_obj, "config_schema", None)
    config_defaults: dict = getattr(cls_obj, "config_defaults", {}) or {}

    fields: set[str] = set(config_defaults.keys())

    if config_schema:
        if "properties" in config_schema:
            fields.update(config_schema["properties"].keys())
        else:
            _meta = {"type", "required", "additionalProperties", "description", "title"}
            for k in config_schema:
                if k not in _meta:
                    fields.add(k)

    return sorted(fields)


def _field_info(field: str, cls_obj: type) -> dict:
    """Return {type?, description?, sensitive?, default?} for a config field."""
    config_schema: Optional[dict] = getattr(cls_obj, "config_schema", None)
    config_defaults: dict = getattr(cls_obj, "config_defaults", {}) or {}

    info: dict = {}

    if config_schema:
        if "properties" in config_schema:
            raw = config_schema["properties"].get(field, {})
        else:
            raw = config_schema.get(field, {})
        if isinstance(raw, dict):
            info["type"] = raw.get("type")
            info["description"] = raw.get("description")
            info["sensitive"] = raw.get("sensitive", False)

    if field in config_defaults:
        info["default"] = config_defaults[field]

    return info


def generate_tool_block(tool_cls: type) -> Optional[str]:
    """Return the env comment block for a single tool, or None if no config."""
    fields = _get_all_fields(tool_cls)
    if not fields:
        return None

    tool_name: str = tool_cls.name  # type: ignore[attr-defined]
    env_prefix = f"TOOL_{tool_name.upper()}__"
    lines: list[str] = [_sep(f"tool:{tool_name}")]

    for field in fields:
        info = _field_info(field, tool_cls)
        env_var = f"{env_prefix}{field.upper()}"

        if info.get("description"):
            lines.append(f"# {info['description']}")

        if info.get("sensitive"):
            lines.append(f"# {env_var}=")
        else:
            default = info.get("default")
            lines.append(f"# {env_var}={_format_default(default)}")

    return "\n".join(lines)


def generate_all_tool_blocks(tools: dict[str, type]) -> str:
    """Return the full generated content for the TOOL DEFAULTS zone."""
    blocks: list[str] = []
    for tool_name in sorted(tools.keys()):
        tool_cls = tools[tool_name]
        has_config = (
            getattr(tool_cls, "config_schema", None) is not None
            or bool(getattr(tool_cls, "config_defaults", {}))
        )
        if not has_config:
            continue
        block = generate_tool_block(tool_cls)
        if block:
            blocks.append(block)

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Auth provider discovery & block generation
# ---------------------------------------------------------------------------

def _collect_auth_subclasses(cls: type, result: dict[str, type]) -> None:
    """Recursively collect all concrete BaseAuthProvider subclasses."""
    name_attr = cls.__dict__.get("name") or getattr(cls, "name", None)
    if (
        not inspect.isabstract(cls)
        and isinstance(name_attr, str)
        and name_attr not in result
    ):
        result[name_attr] = cls
    for sub in cls.__subclasses__():
        _collect_auth_subclasses(sub, result)


def discover_auth_providers() -> dict[str, type]:
    """Import all auth backend packages and return {provider_name: class} mapping."""
    try:
        from src.shared.auth.base import BaseAuthProvider  # noqa: F401
    except ImportError as exc:
        print(f"  [skip] auth base import failed: {exc}")
        return {}

    for pkg_name in _AUTH_PACKAGES:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError as exc:
            print(f"  [skip] {pkg_name}: {exc}")
            continue

        pkg_paths = getattr(pkg, "__path__", None)
        if not pkg_paths:
            continue

        for _finder, mod_name, _ispkg in pkgutil.walk_packages(
            pkg_paths, prefix=f"{pkg_name}."
        ):
            try:
                importlib.import_module(mod_name)
            except Exception as exc:
                print(f"  [skip] {mod_name}: {exc}")

    from src.shared.auth.base import BaseAuthProvider as _BaseAuthProvider

    providers: dict[str, type] = {}
    _collect_auth_subclasses(_BaseAuthProvider, providers)
    return providers


def generate_auth_block(provider_cls: type) -> Optional[str]:
    """Return the env comment block for a single auth provider, or None if no config."""
    fields = _get_all_fields(provider_cls)
    if not fields:
        return None

    provider_name: str = provider_cls.name  # type: ignore[attr-defined]
    env_prefix = f"AUTH_{provider_name.upper()}__"
    lines: list[str] = [_sep(f"auth_provider:{provider_name}")]

    for field in fields:
        # Skip internal injection keys (underscore-prefixed)
        if field.startswith("_"):
            continue
        info = _field_info(field, provider_cls)
        env_var = f"{env_prefix}{field.upper()}"

        if info.get("description"):
            lines.append(f"# {info['description']}")

        if info.get("sensitive"):
            lines.append(f"# {env_var}=")
        else:
            default = info.get("default")
            lines.append(f"# {env_var}={_format_default(default)}")

    return "\n".join(lines)


def generate_all_auth_blocks(providers: dict[str, type]) -> str:
    """Return the full generated content for the AUTH PROVIDER DEFAULTS zone."""
    blocks: list[str] = []
    for provider_name in sorted(providers.keys()):
        provider_cls = providers[provider_name]
        available: bool = getattr(provider_cls, "available", True)
        if not available:
            continue  # skip disabled example providers
        has_config = (
            getattr(provider_cls, "config_schema", None) is not None
            or bool(getattr(provider_cls, "config_defaults", {}))
        )
        if not has_config:
            continue
        block = generate_auth_block(provider_cls)
        if block:
            blocks.append(block)

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# .env.example update
# ---------------------------------------------------------------------------

def _rewrite_zone(
    content: str,
    marker_begin: str,
    marker_end: str,
    new_zone: str,
    zone_label: str,
) -> str:
    """Replace the content between *marker_begin* and *marker_end* with *new_zone*."""
    begin_idx = content.find(marker_begin)
    end_idx = content.find(marker_end)

    if begin_idx == -1 or end_idx == -1:
        print(f"WARNING: {zone_label} markers not found in .env.example — skipping.")
        print(f"  Add these lines to .env.example first:")
        print(f"  {marker_begin}")
        print(f"  {marker_end}")
        return content

    return (
        content[:begin_idx]
        + marker_begin + "\n"
        + (new_zone + "\n\n" if new_zone else "")
        + marker_end
        + content[end_idx + len(marker_end):]
    )


def update_env_example(env_example_path: Path, *, dry_run: bool = False) -> None:
    content = env_example_path.read_text()

    # ── Tool zone ─────────────────────────────────────────────────────────
    print("Discovering tools…")
    tools = discover_tools()
    print(f"Found {len(tools)} tools with config declarations.")
    tool_zone = generate_all_tool_blocks(tools)
    content = _rewrite_zone(
        content, TOOL_MARKER_BEGIN, TOOL_MARKER_END, tool_zone, "TOOL DEFAULTS"
    )

    # ── Auth provider zone ────────────────────────────────────────────────
    print("Discovering auth providers…")
    providers = discover_auth_providers()
    print(f"Found {len(providers)} auth providers.")
    auth_zone = generate_all_auth_blocks(providers)
    content = _rewrite_zone(
        content, AUTH_MARKER_BEGIN, AUTH_MARKER_END, auth_zone, "AUTH PROVIDER DEFAULTS"
    )

    if dry_run:
        print("─── DRY RUN — would write: ───")
        for marker in (TOOL_MARKER_BEGIN, AUTH_MARKER_BEGIN):
            zone_start = content.find(marker)
            end_marker = TOOL_MARKER_END if "TOOL" in marker else AUTH_MARKER_END
            zone_end = content.find(end_marker) + len(end_marker)
            if zone_start != -1:
                print(content[zone_start:zone_end])
        print("─── end of dry run ───")
    else:
        env_example_path.write_text(content)
        print(f"Updated {env_example_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    env_example = ROOT / ".env.example"
    update_env_example(env_example, dry_run=dry_run)
    print("Done.")
