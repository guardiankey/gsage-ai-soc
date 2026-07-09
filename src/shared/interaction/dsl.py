"""gSage AI — Form → InteractionSchema builder (DSL serializer)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.shared.interaction.field_base import InteractionSchema

if TYPE_CHECKING:
    from src.shared.interaction.form import Form


def build_interaction_schema(form_cls: type["Form"]) -> InteractionSchema:
    """Convert a :class:`Form` subclass to a typed :class:`InteractionSchema`.

    Args:
        form_cls: A ``Form`` subclass with ``BaseField`` attributes.

    Returns:
        ``InteractionSchema`` with ``interaction_type="form"`` and the
        field list populated from the form's declared fields.
    """
    field_schemas = [
        field.to_schema() for field in form_cls._fields.values()
    ]
    return InteractionSchema(
        interaction_type="form",
        fields=field_schemas,
    )
