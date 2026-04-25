"""gSage AI — pluggable authentication provider system."""

from src.shared.auth.base import (
    AuthErrorType,
    AuthIdentity,
    AuthResult,
    BaseAuthProvider,
)
from src.shared.auth.registry import get_registry

__all__ = [
    "AuthErrorType",
    "AuthIdentity",
    "AuthResult",
    "BaseAuthProvider",
    "get_registry",
]
