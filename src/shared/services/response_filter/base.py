"""Base types for the response filter pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    ClassVar,
    Literal,
    Optional,
    Protocol,
    Union,
    runtime_checkable,
)
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# Granularity values:
#   * "global"             — filter receives the full text once.
#   * "fenced_block:<lang>" — filter receives only the inner content of
#     ``` <lang> ... ``` blocks (one call per block).
Granularity = Union[Literal["global"], str]


@dataclass(slots=True)
class FilterContext:
    """Request-scoped context passed to every filter.

    Most filters will not need any of these fields, but they are kept on
    the context so future filters (PII masking, tenant-specific term
    redaction, etc.) can opt in without changing the pipeline signature.
    """

    org_id: Optional[UUID] = None
    interface: Optional[str] = None  # "web" | "teams" | "telegram" | "email" | "cli"
    direction: Literal["outbound", "inbound"] = "outbound"
    db: Optional["AsyncSession"] = None


@runtime_checkable
class ResponseFilter(Protocol):
    """Protocol implemented by every response filter.

    ``name`` is used for logging only. ``granularity`` decides what slice
    of text the pipeline hands to :meth:`apply`:

    * ``"global"`` — full text (full-text mode) or post-stream remainder
      (streaming mode).
    * ``"fenced_block:<lang>"`` — the inner content (without the
      surrounding ``` ``` `` fences) of every matching block, one call
      per block.

    ``apply`` MUST be deterministic and side-effect free; it MAY perform
    awaits (e.g. DB lookups via ``ctx.db``) but should keep latency low
    because it runs on every response.
    """

    name: ClassVar[str]
    granularity: ClassVar[Granularity]

    async def apply(self, text: str, ctx: FilterContext) -> str: ...
