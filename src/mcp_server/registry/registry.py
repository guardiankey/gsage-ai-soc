"""gSage AI — Tool Registry."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import pkgutil
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.ext.asyncio import async_sessionmaker

from src.mcp_server.tools.base import BaseTool
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    Global tool registry for metadata, permission filtering, and discovery.

    Dynamic filtering (CRITICAL per PROMPT.md Phase 4):
        - ``get_tools(agent_context)`` returns ONLY tools matching user permissions.
        - The LLM MUST never see tools the user is not authorized to execute.
        - Tool list is generated per-request from AgentContext.permissions.

    Versioning:
        - All versions are stored: ``dns_lookup@1.0.0``, ``dns_lookup@1.1.0`` etc.
        - Latest version is default for ``get_tool(name)``.
        - Older versions remain callable via ``get_tool(name, version)``.
    """

    def __init__(self) -> None:
        # {name: {version: tool_instance}}
        self._tools: dict[str, dict[str, BaseTool]] = {}
        # {name: latest_version}
        self._latest: dict[str, str] = {}

    def register(self, tool: BaseTool) -> None:
        """
        Register a tool instance.

        Args:
            tool: Instantiated BaseTool subclass.

        Raises:
            ValueError: If a tool with same name+version is already registered.
        """
        name = tool.name
        version = tool.version

        if name not in self._tools:
            self._tools[name] = {}

        if version in self._tools[name]:
            raise ValueError(
                f"Tool '{name}@{version}' is already registered. "
                "Increment version for a new registration."
            )

        self._tools[name][version] = tool
        self._latest[name] = version  # Last registered is latest
        logger.info("Tool registered: %s@%s (permissions: %s)", name, version, tool.permissions)

    def get_tool(
        self,
        name: str,
        version: Optional[str] = None,
    ) -> Optional[BaseTool]:
        """
        Get a specific tool by name (and optionally version).

        Args:
            name: Tool name (e.g., "dns_lookup").
            version: Semver string. If None, returns latest.

        Returns:
            Tool instance or None if not found.
        """
        if name not in self._tools:
            return None

        target_version = version or self._latest.get(name)
        if not target_version:
            return None

        return self._tools[name].get(target_version)

    def get_tools(self, agent_context: AgentContext) -> list[BaseTool]:
        """
        Get all tools visible to this user based on their permissions.

        This is the PRIMARY access control filter for the LLM.
        Only tools where at least ONE required permission matches
        the user's permission set are returned.

        Args:
            agent_context: User's request context with resolved permissions.

        Returns:
            List of latest-version tool instances the user may use.

        Security:
            NEVER expose unauthorized tools — not even their names.
        """
        user_permissions = list(agent_context.permissions)
        visible: list[BaseTool] = []

        for name, versions in self._tools.items():
            latest_version = self._latest.get(name)
            if not latest_version:
                continue

            tool = versions[latest_version]

            # Tools with no required permissions are always visible to any
            # authenticated user (e.g. the search_tools meta-tool).
            if not tool.permissions:
                visible.append(tool)
                continue

            # A tool is visible if any granted permission (glob) matches any
            # required permission of the tool, or vice-versa (e.g. tool
            # requires "dns:read" and user has "dns:*").
            if any(
                fnmatch(required, granted)
                for required in tool.permissions
                for granted in user_permissions
            ):
                visible.append(tool)

        return visible

    def get_tool_schemas(self, agent_context: AgentContext) -> list[dict]:
        """
        Get tool schemas for visible tools (for LLM tool definitions).

        Args:
            agent_context: User's request context.

        Returns:
            List of tool schema dicts for LLM consumption.
        """
        return [
            {
                "name": tool.name,
                "version": tool.version,
                "permissions": tool.permissions,
                "rate_limit_per_minute": tool.rate_limit_per_minute,
                "timeout_seconds": tool.timeout_seconds,
                "params_schema": tool.params_schema,
                "config_schema": tool.config_schema,
                "state_schema": tool.state_schema,
            }
            for tool in self.get_tools(agent_context)
        ]

    def list_all(self) -> list[dict]:
        """
        List all registered tools with full metadata (admin use only).

        Returns:
            List of dicts with tool metadata for all versions.
        """
        result = []
        for name, versions in self._tools.items():
            for version, tool in versions.items():
                result.append({
                    "name": name,
                    "version": version,
                    "is_latest": self._latest.get(name) == version,
                    "permissions": tool.permissions,
                    "rate_limit_per_minute": tool.rate_limit_per_minute,
                    "timeout_seconds": tool.timeout_seconds,
                    "use_circuit_breaker": tool.use_circuit_breaker,
                    "requires_config": tool.requires_config,
                    "reset_policy": tool.reset_policy,
                })
        return result

    def __len__(self) -> int:
        """Number of registered tool names (not counting versions)."""
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        """Check if a tool name is registered."""
        return name in self._tools


# ── Singleton registry ──────────────────────────────────────────────────────

_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    """Get or create the global tool registry singleton."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


def build_registry() -> ToolRegistry:
    """
    Build and populate the tool registry by auto-discovering all BaseTool
    subclasses in ``src.mcp_server.tools`` sub-packages.

    Discovery scans every sub-package (e.g. ``core/``, ``network/``,
    ``threat_intel/``) and registers one instance of each concrete
    BaseTool subclass found.  Infrastructure modules (``base``, ``audit``,
    ``circuit_breaker``) live at the package root and are skipped.

    To add a new tool:
        1. Create a module in the appropriate sub-folder.
        2. Define a BaseTool subclass with ``name`` ClassVar.
        3. Done — ``build_registry()`` picks it up automatically.

    Custom tools (``CUSTOM_TOOLS_MODULE`` setting):
        Place custom BaseTool subclasses in the configured Python package
        (default: ``custom_code.tools``).  The package is mounted as a Docker
        volume; if it is not present the registry skips it with a warning.
        Sub-directories are supported if they contain an ``__init__.py``.

        A YAML file with the same stem alongside a tool module supplies
        ``config_defaults`` overrides (class definition wins on collision).

    Called once at application startup.

    Returns:
        Populated ToolRegistry instance.
    """
    from src.shared.config.settings import get_settings
    from src.mcp_server.registry._tool_filter import (
        describe_patterns,
        parse_patterns,
    )
    settings = get_settings()

    registry = get_registry()

    # ── Parse filtering patterns (TOOLS_ENABLED / TOOLS_DISABLED) ───────────
    enabled_patterns = parse_patterns(settings.tools_enabled or "")
    disabled_patterns = parse_patterns(settings.tools_disabled or "")
    if enabled_patterns or disabled_patterns:
        logger.info(
            "Tool filter active — enabled=%s disabled=%s",
            describe_patterns(enabled_patterns) or "ALL",
            describe_patterns(disabled_patterns) or "NONE",
        )

    _SKIP_MODULES = frozenset(("base", "audit", "circuit_breaker", "crud_base"))

    # ── Built-in tools ──────────────────────────────────────────────────────
    import src.mcp_server.tools as tools_pkg

    _register_tools_from_pkg(
        registry,
        pkg=tools_pkg,
        pkg_name=tools_pkg.__name__,
        skip_modules=_SKIP_MODULES,
        skip_if_crud_disabled=True,
        crud_tools_enabled=settings.crud_tools_enabled,
        enabled_patterns=enabled_patterns,
        disabled_patterns=disabled_patterns,
    )

    # ── Custom tools (optional volume mount) ────────────────────────────────
    custom_module = (settings.custom_tools_module or "").strip()
    if custom_module:
        try:
            custom_pkg = importlib.import_module(custom_module)
        except ModuleNotFoundError:
            logger.warning(
                "Custom tools package '%s' not found — skipping. "
                "Mount the volume or set CUSTOM_TOOLS_MODULE='' to suppress.",
                custom_module,
            )
            custom_pkg = None
        except Exception:
            logger.exception("Failed to import custom tools package '%s'", custom_module)
            custom_pkg = None

        if custom_pkg is not None:
            _register_tools_from_pkg(
                registry,
                pkg=custom_pkg,
                pkg_name=custom_module,
                skip_modules=frozenset(),
                skip_if_crud_disabled=False,
                crud_tools_enabled=True,
                enabled_patterns=enabled_patterns,
                disabled_patterns=disabled_patterns,
            )

    logger.info("Tool registry built: %d tools registered", len(registry))
    return registry


async def sync_permissions_to_db(
    registry: ToolRegistry,
    session_factory: "async_sessionmaker[AsyncSession]",
) -> None:
    """Sync tool permission tags to the ``gsage_permissions`` table.

    Runs once at MCP server startup right after ``build_registry()``.
    Ensures every permission tag declared via ``BaseTool.permissions``
    ClassVar has a corresponding row in the database so the admin
    console can list and assign them to groups.

    Behaviour:
        - **Insert** any tag not yet present in the DB.  Description is
          auto-generated listing all tools that require the tag.
        - **Re-activate** any tag previously marked as ``"deprecated"``
          that is declared again by a tool.
        - **Deprecate** any tag that exists in the DB but is no longer
          declared by any tool (sets ``category = "deprecated"`` and
          logs a warning — the row is kept so existing group assignments
          are not silently broken).
        - Idempotent — safe to call on every startup.

    Args:
        registry: Populated ``ToolRegistry`` (after ``build_registry()``).
        session_factory: ``async_sessionmaker`` instance for DB access.
    """
    from sqlalchemy import select as _select
    from src.shared.models.permission import GSagePermission

    # Collect every tag declared by the latest version of each tool.
    # Build a mapping tag → list of tool names for descriptive text.
    tag_to_tools: dict[str, list[str]] = {}
    for name, versions in registry._tools.items():
        latest_version = registry._latest.get(name)
        if not latest_version:
            continue
        tool = versions[latest_version]
        for tag in tool.permissions:
            if tag == "*":
                continue  # wildcard is managed exclusively by bootstrap
            tag_to_tools.setdefault(tag, []).append(tool.name)

    # Generate synthetic wildcard tags by stripping the last segment of each
    # declared tag.  Example: "files:read" + "files:write" → "files:*".
    # Only add a wildcard when it is not already declared by a tool itself.
    wildcard_to_tools: dict[str, set[str]] = {}
    for tag, tools in tag_to_tools.items():
        parts = tag.split(":")
        if len(parts) >= 2:
            wc = ":".join(parts[:-1]) + ":*"
            if wc not in tag_to_tools:
                wildcard_to_tools.setdefault(wc, set()).update(tools)

    for wc, tools in wildcard_to_tools.items():
        tag_to_tools[wc] = sorted(tools)

    async with session_factory() as db:
        result = await db.execute(_select(GSagePermission))
        existing: dict[str, GSagePermission] = {
            p.tag: p for p in result.scalars().all()
        }

        new_count = 0
        reactivated_count = 0
        for tag, tools in sorted(tag_to_tools.items()):
            if tag in existing:
                perm = existing[tag]
                if perm.category == "deprecated":
                    # Tag is back — un-deprecate it
                    perm.category = tag.split(":")[0]
                    perm.description = f"Required by: {', '.join(sorted(tools))}"
                    logger.info(
                        "Permission '%s' re-activated (was deprecated, now required by: %s)",
                        tag,
                        ", ".join(sorted(tools)),
                    )
                    reactivated_count += 1
                continue

            category = tag.split(":")[0]
            tool_list = ", ".join(sorted(tools))
            is_wildcard = tag.endswith(":*")
            description = (
                f"Wildcard — grants all {tag} permissions (tools: {tool_list})"
                if is_wildcard
                else f"Required by: {tool_list}"
            )
            db.add(
                GSagePermission(
                    tag=tag,
                    description=description,
                    category=category,
                )
            )
            logger.debug("Permission '%s' added (category: %s, tools: %s)", tag, category, tool_list)
            new_count += 1

        # Mark permissions that no tool declares anymore as deprecated
        declared_tags = set(tag_to_tools.keys())
        orphaned_count = 0
        for tag, perm in existing.items():
            if tag == "*":
                continue
            if tag not in declared_tags and perm.category != "deprecated":
                perm.category = "deprecated"
                logger.warning(
                    "Permission '%s' marked as deprecated — no registered tool declares it",
                    tag,
                )
                orphaned_count += 1

        if new_count or reactivated_count or orphaned_count:
            await db.commit()
            logger.info(
                "Permission sync complete — %d inserted, %d re-activated, %d deprecated",
                new_count,
                reactivated_count,
                orphaned_count,
            )
        else:
            logger.debug("Permission sync complete — no changes needed")


async def sync_tools_to_db(
    registry: ToolRegistry,
    session_factory: "async_sessionmaker[AsyncSession]",
) -> None:
    """Sync in-memory tool registry to the ``gsage_tools`` table.

    Runs once at MCP server startup right after ``build_registry()``.
    Keeps the DB in sync with the set of tools discovered at runtime so
    that admin UIs (e.g. tool-config dropdowns) can list available tools
    without depending on in-memory state.

    Behaviour:
        - **Insert** any tool not yet present in the DB.
        - **Update** existing rows with the latest metadata (version,
          schemas, rate limits, etc.).
        - **Deactivate** any tool that exists in the DB but is no longer
          registered (sets ``is_active = False`` — the row is kept so
          existing tool configs remain intact).
        - Idempotent — safe to call on every startup.

    Args:
        registry: Populated ``ToolRegistry`` (after ``build_registry()``).
        session_factory: ``async_sessionmaker`` instance for DB access.
    """
    from sqlalchemy import select as _select
    from src.shared.models.tool import GSageTool

    async with session_factory() as db:
        result = await db.execute(_select(GSageTool))
        existing: dict[str, GSageTool] = {
            gt.name: gt for gt in result.scalars().all()
        }

        active_names: set[str] = set()
        new_count = 0
        updated_count = 0

        for name, versions in registry._tools.items():
            latest_version = registry._latest.get(name)
            if not latest_version:
                continue
            tool = versions[latest_version]
            active_names.add(name)

            # Derive human-readable metadata from tool class attributes.
            display_name = name.replace("_", " ").title()
            raw_doc = (tool.__class__.__doc__ or "").strip()
            description = raw_doc.splitlines()[0].strip() if raw_doc else display_name
            # Use explicit category/summary ClassVars; fall back to legacy derivation.
            category = tool.category if tool.category != "general" or tool.permissions else (
                tool.permissions[0].split(":")[0] if tool.permissions else "general"
            )
            summary = tool.summary or description

            if name in existing:
                gt = existing[name]
                gt.version = latest_version
                gt.display_name = display_name
                gt.description = description
                gt.summary = summary
                gt.category = category
                gt.required_permissions = list(tool.permissions)
                gt.input_schema = tool.params_schema or {}
                gt.config_schema = tool.config_schema
                gt.config_defaults = dict(tool.config_defaults) if tool.config_defaults else None
                gt.state_schema = tool.state_schema
                gt.state_defaults = dict(tool.state_defaults) if tool.state_defaults else None
                gt.reset_policy = tool.reset_policy
                gt.timeout_seconds = tool.timeout_seconds
                gt.rate_limit_per_minute = tool.rate_limit_per_minute
                gt.requires_config = tool.requires_config
                gt.requires_user_credentials = getattr(tool, "requires_user_credentials", False)
                gt.credential_namespace = getattr(tool, "credential_namespace", None)
                gt.credential_schema = getattr(tool, "credential_schema", None)
                gt.config_namespace = getattr(tool, "config_namespace", None)
                gt.is_active = True
                updated_count += 1
            else:
                db.add(GSageTool(
                    name=name,
                    version=latest_version,
                    display_name=display_name,
                    description=description,
                    summary=summary,
                    category=category,
                    required_permissions=list(tool.permissions),
                    input_schema=tool.params_schema or {},
                    output_schema={},
                    config_schema=tool.config_schema,
                    config_defaults=dict(tool.config_defaults) if tool.config_defaults else None,
                    state_schema=tool.state_schema,
                    state_defaults=dict(tool.state_defaults) if tool.state_defaults else None,
                    reset_policy=tool.reset_policy,
                    timeout_seconds=tool.timeout_seconds,
                    rate_limit_per_minute=tool.rate_limit_per_minute,
                    requires_config=tool.requires_config,
                    requires_user_credentials=getattr(tool, "requires_user_credentials", False),
                    credential_namespace=getattr(tool, "credential_namespace", None),
                    credential_schema=getattr(tool, "credential_schema", None),
                    config_namespace=getattr(tool, "config_namespace", None),
                    is_active=True,
                ))
                logger.debug("Tool '%s@%s' added to DB", name, latest_version)
                new_count += 1

        # Deactivate tools that are no longer in the registry.
        deactivated_count = 0
        for name, gt in existing.items():
            if name not in active_names and gt.is_active:
                gt.is_active = False
                logger.warning("Tool '%s' deactivated — no longer in registry", name)
                deactivated_count += 1

        if new_count or updated_count or deactivated_count:
            await db.commit()
            logger.info(
                "Tool sync complete — %d inserted, %d updated, %d deactivated",
                new_count,
                updated_count,
                deactivated_count,
            )
        else:
            logger.debug("Tool sync complete — no changes needed")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _apply_yaml_defaults(tool_cls: type, module_file: Optional[str]) -> None:
    """
    Merge YAML config defaults into ``tool_cls.config_defaults``.

    Looks for a ``.yaml`` file with the same stem as *module_file*.
    Values already set in the class definition are NOT overridden (class wins).

    Args:
        tool_cls: Concrete BaseTool subclass.
        module_file: ``__file__`` of the module that defines the tool.
    """
    if not module_file:
        return
    yaml_path = Path(module_file).with_suffix(".yaml")
    if not yaml_path.is_file():
        return
    try:
        import yaml  # optional dependency — only needed when YAML files exist
        with yaml_path.open() as fh:
            yaml_data: dict = yaml.safe_load(fh) or {}
    except Exception:
        logger.warning("Failed to load YAML defaults for %s from %s", tool_cls.name, yaml_path)
        return

    existing: dict = dict(getattr(tool_cls, "config_defaults", {}))
    merged = {**yaml_data, **existing}  # class wins on collision
    tool_cls.config_defaults = merged  # type: ignore[attr-defined]
    logger.debug("YAML defaults applied to %s: %s keys merged", tool_cls.name, len(yaml_data))


def _register_tools_from_pkg(
    registry: ToolRegistry,
    pkg: object,
    pkg_name: str,
    *,
    skip_modules: frozenset,
    skip_if_crud_disabled: bool,
    crud_tools_enabled: bool = True,
    enabled_patterns: "list | None" = None,
    disabled_patterns: "list | None" = None,
) -> None:
    """
    Walk *pkg* with pkgutil and register all concrete BaseTool subclasses.

    Args:
        registry: Target ToolRegistry.
        pkg: Imported package object (must have ``__path__``).
        pkg_name: Dotted package name prefix.
        skip_modules: Module short-names to skip (e.g. ``{"base", "audit"}``).
        skip_if_crud_disabled: When True, skip modules under ``.crud.`` if
            *crud_tools_enabled* is False.
        crud_tools_enabled: Whether CRUD tools are enabled.
        enabled_patterns: Parsed ``TOOLS_ENABLED`` entries (see
            ``_tool_filter.parse_patterns``). ``None`` or empty list means
            "no allow-list filter".
        disabled_patterns: Parsed ``TOOLS_DISABLED`` entries. Deny-list
            always wins over the allow-list.
    """
    from src.mcp_server.registry._tool_filter import (
        module_allowed,
        tool_allowed,
    )

    enabled_patterns = enabled_patterns or []
    disabled_patterns = disabled_patterns or []

    for _finder, module_name, _is_pkg in pkgutil.walk_packages(
        pkg.__path__,  # type: ignore[attr-defined]
        prefix=pkg_name + ".",
    ):
        short = module_name.rsplit(".", 1)[-1]
        if short in skip_modules:
            continue

        if skip_if_crud_disabled and ".crud." in module_name and not crud_tools_enabled:
            logger.debug("CRUD tools disabled — skipping %s", module_name)
            continue

        # Module-level deny/allow (checked BEFORE import to skip heavy
        # dependencies of filtered packages).
        if not module_allowed(module_name, enabled_patterns, disabled_patterns):
            logger.debug("Tool filter: skipping module %s", module_name)
            continue

        try:
            module = importlib.import_module(module_name)
        except Exception:
            logger.exception("Failed to import tool module: %s", module_name)
            continue

        module_file = getattr(module, "__file__", None)

        for _attr_name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, BaseTool)
                and obj is not BaseTool
                and not inspect.isabstract(obj)
                and hasattr(obj, "name")
                and getattr(obj, "available", True)
            ):
                # Tool-level deny/allow (name / category / regex).
                if not tool_allowed(
                    name=getattr(obj, "name", ""),
                    category=getattr(obj, "category", None),
                    module=module_name,
                    core=bool(getattr(obj, "core_tool", False)),
                    enabled=enabled_patterns,
                    disabled=disabled_patterns,
                ):
                    is_core = bool(getattr(obj, "core_tool", False))
                    if is_core:
                        logger.warning(
                            "Tool filter: core tool '%s' (category=%s) excluded by "
                            "TOOLS_ENABLED/TOOLS_DISABLED — this may break "
                            "baseline functionality.",
                            getattr(obj, "name", "?"),
                            getattr(obj, "category", "?"),
                        )
                    else:
                        logger.info(
                            "Tool filter: excluding '%s' (category=%s)",
                            getattr(obj, "name", "?"),
                            getattr(obj, "category", "?"),
                        )
                    continue

                _apply_yaml_defaults(obj, module_file)
                try:
                    registry.register(obj())
                except ValueError:
                    # Already registered (e.g. re-exported in __init__)
                    pass
