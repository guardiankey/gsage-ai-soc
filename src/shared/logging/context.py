"""gSage AI — Trace context for structured logging (Phase 9).

Stores trace_id, org_id, and user_id in Python ``contextvars`` so every
log record emitted within a request/task automatically carries the correct
trace context without passing it explicitly to every function.

Usage
-----
Set at entry points (HTTP middleware, Celery task pre-run)::

    from src.shared.logging.context import set_trace_context
    set_trace_context(trace_id="...", org_id="...", user_id="...")

Read from log filter::

    from src.shared.logging.context import get_trace_id
    trace_id = get_trace_id()   # "unknown" if not set
"""

from __future__ import annotations

from contextvars import ContextVar

# ── Context variables ──────────────────────────────────────────────────────
# Each request/greenlet/coroutine gets its own copy of these vars.

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="unknown")
_org_id_var:   ContextVar[str] = ContextVar("org_id",   default="")
_user_id_var:  ContextVar[str] = ContextVar("user_id",  default="")
_service_var:  ContextVar[str] = ContextVar("service",  default="")


# ── Setters ────────────────────────────────────────────────────────────────


def set_trace_context(
    *,
    trace_id: str,
    org_id: str = "",
    user_id: str = "",
    service: str = "",
) -> None:
    """Bind trace/user context to the current execution context."""
    _trace_id_var.set(trace_id)
    _org_id_var.set(org_id or "")
    _user_id_var.set(user_id or "")
    if service:
        _service_var.set(service)


# ── Getters ────────────────────────────────────────────────────────────────


def get_trace_id() -> str:
    """Return the current trace_id (or "unknown" if not set)."""
    return _trace_id_var.get()


def get_org_id() -> str:
    """Return the current org_id (empty string if not set)."""
    return _org_id_var.get()


def get_user_id() -> str:
    """Return the current user_id (empty string if not set)."""
    return _user_id_var.get()


def get_service() -> str:
    """Return the current service name."""
    return _service_var.get()
