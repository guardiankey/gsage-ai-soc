"""gSage AI — Interaction Service field types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.shared.interaction.field_base import BaseField


@dataclass
class TextField(BaseField):
    """Single-line text input."""

    field_type: str = "text"
    max_length: Optional[int] = None
    min_length: Optional[int] = None

    def _extras(self) -> dict:
        return {
            "max_length": self.max_length,
            "min_length": self.min_length,
        }


@dataclass
class TextAreaField(BaseField):
    """Multi-line text input."""

    field_type: str = "textarea"
    rows: int = 4
    max_length: Optional[int] = None

    def _extras(self) -> dict:
        return {
            "rows": self.rows,
            "max_length": self.max_length,
        }


@dataclass
class NumberField(BaseField):
    """Numeric input with optional min/max/step."""

    field_type: str = "number"
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None

    def _extras(self) -> dict:
        return {
            "min": self.min,
            "max": self.max,
            "step": self.step,
        }


@dataclass
class SelectField(BaseField):
    """Dropdown select (single or multiple)."""

    field_type: str = "select"
    options: list[dict] = field(default_factory=list)
    multiple: bool = False

    def _extras(self) -> dict:
        return {
            "options": self.options,
            "multiple": self.multiple,
        }


@dataclass
class CheckboxField(BaseField):
    """Boolean checkbox."""

    field_type: str = "checkbox"


@dataclass
class CheckboxGroupField(BaseField):
    """Multi-select checkbox group — returns an array of selected values."""

    field_type: str = "checkbox_group"
    options: list[dict] = field(default_factory=list)

    def _extras(self) -> dict:
        return {
            "options": self.options,
        }


@dataclass
class RadioField(BaseField):
    """Radio button group."""

    field_type: str = "radio"
    options: list[dict] = field(default_factory=list)

    def _extras(self) -> dict:
        return {
            "options": self.options,
        }


@dataclass
class DateField(BaseField):
    """Date picker with optional min/max range."""

    field_type: str = "date"
    min_date: Optional[str] = None  # ISO date string
    max_date: Optional[str] = None

    def _extras(self) -> dict:
        return {
            "min_date": self.min_date,
            "max_date": self.max_date,
        }
