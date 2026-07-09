"""gSage AI — BaseField and typed schemas for the Interaction Service."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class BaseField:
    """Common metadata for all field types.

    Design notes:

    * ``value`` pre-fills the field (agent-edited forms, edit mode).
    * Layout metadata (``width``, ``group``, ``order``, ``tab``, ``icon``)
      is reserved for future use; React may ignore it in V1 but the schema
      carries it.
    * ``visible_when`` / ``enabled_when`` are reserved for V2 conditional
      field support; serialized but not evaluated in V1.
    """

    id: str = ""  # auto-set from class attribute name by Form.__init_subclass__
    label: str = ""
    field_type: str = ""  # set by subclass ("text", "number", "select", …)

    description: Optional[str] = None
    hint: Optional[str] = None
    placeholder: Optional[str] = None
    required: bool = False
    value: Optional[Any] = None  # pre-filled value (editing / agent-suggested)
    default: Optional[Any] = None
    example: Optional[str] = None
    validation: Optional[dict] = None  # {"pattern": "...", "min_length": 3, …}
    visible: bool = True
    enabled: bool = True

    # ── Conditional visibility (reserved — React ignores in V1) ──────────
    visible_when: Optional[dict] = None  # {"field": "tipo", "op": "eq", "value": "pf"}
    enabled_when: Optional[dict] = None  # same structure

    # ── Layout metadata (reserved — React ignores in V1) ──────────────────
    width: Optional[str] = None  # "full", "half", "third", "200px"
    group: Optional[str] = None  # logical group for <fieldset>
    order: Optional[int] = None  # explicit ordering hint
    tab: Optional[str] = None  # tab name for multi-tab forms (future)
    icon: Optional[str] = None  # Lucide icon name hint

    # ── Serialization ─────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict for the Interaction Schema DSL."""
        result: dict[str, Any] = {
            "id": self.id,
            "field_type": self.field_type,
            "label": self.label,
            "required": self.required,
            "value": self.value,
            "placeholder": self.placeholder,
            "hint": self.hint,
            "description": self.description,
            "default": self.default,
            "example": self.example,
            "validation": self.validation,
            "visible": self.visible,
            "enabled": self.enabled,
            "visible_when": self.visible_when,
            "enabled_when": self.enabled_when,
            "width": self.width,
            "group": self.group,
            "order": self.order,
            "tab": self.tab,
            "icon": self.icon,
        }
        # Merge type-specific extras from subclass
        extras = self._extras()
        if extras:
            result.update(extras)
        return result

    def _extras(self) -> dict:
        """Override in subclasses to include type-specific properties."""
        return {}

    def to_schema(self) -> "FieldSchema":
        """Convert to a typed :class:`FieldSchema` (internal API)."""
        d = self.to_dict()
        # Known FieldSchema fields — extract explicitly so they don't
        # end up in the catch-all ``extras`` dict.
        known = {
            "id", "field_type", "label", "required", "value", "placeholder",
            "hint", "description", "default", "example", "validation",
            "visible", "enabled", "visible_when", "enabled_when",
            "width", "group", "order", "tab", "icon",
        }
        typed_kwargs: dict = {}
        extras: dict = {}
        for k, v in d.items():
            if k in known:
                typed_kwargs[k] = v
            else:
                extras[k] = v
        typed_kwargs["extras"] = extras
        return FieldSchema(**typed_kwargs)


# ── Typed schemas (internal API — avoid bare dicts) ─────────────────────


@dataclass
class FieldSchema:
    """Typed representation of a single field's metadata.

    Produced by ``BaseField.to_schema()``.  Converted to a plain dict
    only at the serialization boundary (SSE / JSON) via ``to_dict()``.
    """

    id: str
    field_type: str
    label: str = ""
    required: bool = False
    value: Optional[Any] = None
    placeholder: Optional[str] = None
    hint: Optional[str] = None
    description: Optional[str] = None
    default: Optional[Any] = None
    example: Optional[str] = None
    validation: Optional[dict] = None
    visible: bool = True
    enabled: bool = True
    visible_when: Optional[dict] = None
    enabled_when: Optional[dict] = None
    width: Optional[str] = None
    group: Optional[str] = None
    order: Optional[int] = None
    tab: Optional[str] = None
    icon: Optional[str] = None
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to JSON-compatible dict (serialization boundary)."""
        result: dict[str, Any] = {
            "id": self.id,
            "field_type": self.field_type,
            "label": self.label,
            "required": self.required,
            "value": self.value,
            "placeholder": self.placeholder,
            "hint": self.hint,
            "description": self.description,
            "default": self.default,
            "example": self.example,
            "validation": self.validation,
            "visible": self.visible,
            "enabled": self.enabled,
            "visible_when": self.visible_when,
            "enabled_when": self.enabled_when,
            "width": self.width,
            "group": self.group,
            "order": self.order,
            "tab": self.tab,
            "icon": self.icon,
        }
        if self.extras:
            result.update(self.extras)
        return result


@dataclass
class InteractionSchema:
    """Typed representation of a complete interaction payload.

    Built by ``Form.build_schema()``.  Serialized to a plain dict only at the
    SSE / JSON boundary via ``to_dict()``.
    """

    interaction_type: str
    fields: list[FieldSchema] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to JSON-compatible dict (serialization boundary)."""
        return {
            "interaction_type": self.interaction_type,
            "fields": [f.to_dict() for f in self.fields],
        }


@dataclass
class InteractionResponseData:
    """Typed response from a completed interaction.

    Returned by ``InteractionService.request()`` (CONTINUE_TOOL mode).
    The ``data`` dict contains the form responses keyed by field ID.
    """

    interaction_id: str
    status: str  # "submitted" | "cancelled" | "timeout"
    data: dict  # form responses: {"nome": "João", "idade": 30}
    context: Optional[dict] = None  # echo of the context passed to request()
