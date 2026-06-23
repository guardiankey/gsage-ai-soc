"""Shared validation and result helpers for Imperva tools."""

from __future__ import annotations

from typing import Any


class ParamError(ValueError):
    """A caller supplied an invalid action-specific parameter."""


def require(params: dict, field: str) -> str:
    value = params.get(field)
    if isinstance(value, str):
        value = value.strip()
    if value in (None, ""):
        raise ParamError(f"'{field}' is required for this action.")
    return str(value)


def optional_payload(params: dict) -> dict[str, Any]:
    """Return a copy of a caller payload after enforcing an object shape."""
    value = params.get("payload")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ParamError("'payload' must be an object.")
    return dict(value)


def compact(value: Any, *, max_items: int = 200) -> Any:
    """Cap list-sized API responses before returning them to the model."""
    if isinstance(value, list):
        return value[:max_items]
    return value
