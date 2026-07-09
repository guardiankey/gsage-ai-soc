"""gSage AI — Form base class (no metaclass)."""

from __future__ import annotations

from src.shared.interaction.field_base import BaseField, InteractionSchema


class Form:
    """Base class for form definitions.

    Uses ``__init_subclass__`` (no metaclass) for simpler debugging.

    Usage::

        class ClienteForm(Form):
            nome = TextField(label="Nome", required=True, value="João da Silva")
            idade = NumberField(label="Idade", min=18, max=120)
            setor = SelectField(label="Setor", options=[
                {"value": "ti", "label": "TI"},
                {"value": "rh", "label": "RH"},
            ])
    """

    _fields: dict[str, BaseField] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Collect BaseField instances from class attributes."""
        super().__init_subclass__(**kwargs)
        fields: dict[str, BaseField] = {}
        for key, value in cls.__dict__.items():
            if isinstance(value, BaseField):
                value.id = key
                fields[key] = value
        cls._fields = fields

    @classmethod
    def build_schema(cls) -> InteractionSchema:
        """Convert this Form to a typed :class:`InteractionSchema`.

        Use ``.to_dict()`` on the result for the JSON-compatible dict
        at the serialization boundary (SSE / DB storage).
        """
        from src.shared.interaction.dsl import build_interaction_schema

        return build_interaction_schema(cls)

    @classmethod
    def to_dict(cls) -> dict:
        """Serialization-boundary shortcut: ``build_schema().to_dict()``."""
        return cls.build_schema().to_dict()
