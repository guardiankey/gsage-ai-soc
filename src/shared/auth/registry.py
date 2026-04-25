"""gSage AI — AuthProvider registry and chain runner.

The registry discovers providers from:
  1. ``src.shared.auth.backends``  — built-in providers
  2. ``custom_code.auth_backends`` — operator-supplied providers

Auto-discovery imports all modules in those packages; any class that is a
concrete subclass of BaseAuthProvider and has a ``name`` attribute is
registered automatically.

Usage::

    registry = get_registry()
    result = await registry.authenticate_chain(
        providers=["ldap", "local"],
        provider_configs={"ldap": {...}, "local": {}},
        username="john@corp.com",
        password="s3cr3t",
    )
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from functools import lru_cache
from typing import Optional

from src.shared.auth.base import (
    AuthErrorType,
    AuthResult,
    BaseAuthProvider,
)

logger = logging.getLogger(__name__)

# Packages scanned for BaseAuthProvider subclasses (in discovery order)
_PROVIDER_PACKAGES = [
    "src.shared.auth.backends",
    "custom_code.auth_backends",
]


class AuthProviderRegistry:
    """Registry that holds and chains authentication providers."""

    def __init__(self) -> None:
        self._providers: dict[str, BaseAuthProvider] = {}

    def register(self, provider: BaseAuthProvider) -> None:
        """Register a provider instance under its ``name``."""
        if provider.name in self._providers:
            logger.warning(
                "AuthProviderRegistry: overwriting existing provider '%s'",
                provider.name,
            )
        self._providers[provider.name] = provider
        logger.debug("AuthProviderRegistry: registered provider '%s'", provider.name)

    def get(self, name: str) -> Optional[BaseAuthProvider]:
        """Return the provider registered under *name*, or None."""
        return self._providers.get(name)

    def available(self) -> list[str]:
        """Return sorted list of registered provider names."""
        return sorted(self._providers.keys())

    def _merge_config(
        self, provider: BaseAuthProvider, org_config: dict
    ) -> dict:
        """Merge config layers: defaults < env vars < org DB config."""
        env_defaults = provider._load_env_defaults()
        return {**provider.config_defaults, **env_defaults, **org_config}

    async def authenticate_chain(
        self,
        providers: list[str],
        provider_configs: dict[str, dict],
        username: str,
        password: str,
    ) -> AuthResult:
        """Attempt authentication through an ordered list of providers.

        Chain rules
        -----------
        - ``result.should_stop_chain`` → stop immediately (success or
          definitive rejection: wrong password, locked, disabled).
        - ``USER_NOT_FOUND`` / ``PROVIDER_UNAVAILABLE`` /
          ``CONFIGURATION_ERROR`` → log and try the next provider.
        - All providers exhausted → return the last failure (or a generic
          USER_NOT_FOUND if no provider was tried).

        Parameters
        ----------
        providers:
            Ordered list of provider names, e.g. ``["ldap", "local"]``.
        provider_configs:
            Per-provider raw org config dict (un-merged).
        username:
            Login identifier submitted by the user.
        password:
            Plain-text password.
        """
        last_result: Optional[AuthResult] = None

        for provider_name in providers:
            provider = self.get(provider_name)
            if provider is None:
                logger.warning(
                    "Auth chain: provider '%s' not registered — skipping",
                    provider_name,
                )
                continue

            config = self._merge_config(
                provider, provider_configs.get(provider_name) or {}
            )

            try:
                result = await provider.authenticate(username, password, config)
            except Exception as exc:
                logger.error(
                    "Auth chain: provider '%s' raised unexpectedly for user '%s': %s",
                    provider_name, username, exc, exc_info=True,
                )
                last_result = AuthResult(
                    success=False,
                    error_type=AuthErrorType.PROVIDER_UNAVAILABLE,
                    error_message=f"Provider '{provider_name}' error: {exc}",
                )
                continue

            result.provider_name = provider_name

            if result.should_stop_chain:
                logger.info(
                    "Auth chain: provider '%s' %s for user '%s'",
                    provider_name,
                    "succeeded" if result.success else f"definitively rejected ({result.error_type})",
                    username,
                )
                return result

            logger.info(
                "Auth chain: provider '%s' returned %s for user '%s' — trying next",
                provider_name, result.error_type, username,
            )
            last_result = result

        return last_result or AuthResult(
            success=False,
            error_type=AuthErrorType.USER_NOT_FOUND,
            error_message="No authentication provider could authenticate this user",
        )

    def _discover(self) -> None:
        """Scan provider packages and auto-register all concrete providers."""
        # Import base to ensure the class hierarchy is rooted
        from src.shared.auth.base import BaseAuthProvider as _Base  # noqa: F401

        for pkg_name in _PROVIDER_PACKAGES:
            try:
                pkg = importlib.import_module(pkg_name)
            except ImportError as exc:
                logger.debug("Auth discovery: skipping package '%s': %s", pkg_name, exc)
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
                    logger.warning(
                        "Auth discovery: could not import '%s': %s", mod_name, exc
                    )

        # Register all discovered concrete subclasses
        def _register_subclasses(cls: type) -> None:
            name_attr = cls.__dict__.get("name") or getattr(cls, "name", None)
            if (
                not inspect.isabstract(cls)
                and isinstance(name_attr, str)
                and name_attr not in self._providers
            ):
                try:
                    self.register(cls())
                except Exception as exc:
                    logger.warning(
                        "Auth discovery: could not instantiate '%s': %s", cls, exc
                    )
            for sub in cls.__subclasses__():
                _register_subclasses(sub)

        _register_subclasses(BaseAuthProvider)


@lru_cache(maxsize=1)
def get_registry() -> AuthProviderRegistry:
    """Return the singleton AuthProviderRegistry (with providers auto-discovered)."""
    registry = AuthProviderRegistry()
    registry._discover()
    return registry
