"""gSage AI — Interaction type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.shared.interaction.enums import InteractionType


@dataclass(kw_only=True)
class BaseInteraction:
    """Base for all interaction types (Form, Confirm, Upload, …).

    Note: ``context`` (audit metadata) is NOT a field of the interaction —
    it is passed to ``InteractionService.request()`` so all interaction
    types share the same mechanism.
    """

    interaction_type: InteractionType

    title: str
    description: str = ""
    timeout_seconds: int = 600

    # ── UI hints (frontend decides how to render) ──────────────────────────
    submit_label: str = ""  # custom submit button text (empty = default)
    cancel_label: str = ""  # custom cancel button text (empty = default)
    size: str = "md"  # "sm" | "md" | "lg" | "xl"

    def to_dict(self) -> dict:
        """Serialize for the SSE event + DB storage."""
        raise NotImplementedError


@dataclass(kw_only=True)
class FormInteraction(BaseInteraction):
    """Form interaction — the only type implemented in V1."""

    interaction_type: InteractionType = InteractionType.FORM
    form: Optional[type] = None  # Form subclass
    schema_override: Optional[dict] = None  # pre-built schema (alternative)

    def to_dict(self) -> dict:
        if self.schema_override:
            schema = self.schema_override
        elif self.form is not None:
            schema = self.form.build_schema().to_dict()
        else:
            raise ValueError(
                "FormInteraction requires `form` or `schema_override`."
            )

        return {
            "interaction_type": self.interaction_type.value,
            "title": self.title,
            "description": self.description,
            "timeout_seconds": self.timeout_seconds,
            "submit_label": self.submit_label or None,
            "cancel_label": self.cancel_label or None,
            "size": self.size,
            "fields": schema.get("fields", []),
        }
