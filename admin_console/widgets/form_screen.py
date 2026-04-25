"""FormScreen — base ModalScreen for CRUD forms."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Switch, TextArea


class FormField:
    """Descriptor for a single form field."""

    def __init__(
        self,
        key: str,
        label: str,
        field_type: str = "text",  # "text" | "password" | "select" | "switch" | "textarea"
        options: list[tuple[str, Any]] | None = None,
        required: bool = False,
        default: Any = None,
        placeholder: str = "",
    ) -> None:
        self.key = key
        self.label = label
        self.field_type = field_type
        self.options = options or []
        self.required = required
        self.default = default
        self.placeholder = placeholder


class FormScreen(ModalScreen[dict[str, Any] | None]):
    """Base class for CRUD form modals.

    Subclasses define :attr:`FIELDS` and optionally override :meth:`validate`.

    Returns ``None`` on cancel, or dict of field values on submit.

    Usage::

        class OrgForm(FormScreen):
            TITLE = "Organization"
            FIELDS = [
                FormField("name", "Name", required=True),
                FormField("slug", "Slug", required=True),
            ]

        result = await self.app.push_screen_wait(OrgForm(initial={"name": "Acme"}))
    """

    DEFAULT_CSS = """
    FormScreen {
        align: center middle;
    }
    FormScreen > Vertical {
        background: #2e3436;
        border: round #729fcf;
        padding: 1 2;
        width: 64;
        height: auto;
        max-height: 80vh;
    }
    FormScreen #form-title {
        text-align: center;
        color: #729fcf;
        text-style: bold;
        height: 1;
        margin-bottom: 1;
    }
    FormScreen .field-label {
        color: #babdb6;
        height: 1;
        margin-top: 1;
    }
    FormScreen .field-label.-required {
        color: #fcaf3e;
    }
    FormScreen Input, FormScreen Select, FormScreen TextArea {
        margin-bottom: 0;
    }
    FormScreen TextArea {
        height: 8;
    }
    FormScreen #error-label {
        color: #ef2929;
        height: 1;
        margin-top: 1;
    }
    FormScreen #btn-row {
        layout: horizontal;
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    FormScreen #btn-row Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    TITLE: str = "Form"
    FIELDS: list[FormField] = []

    def __init__(
        self,
        initial: dict[str, Any] | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._initial = initial or {}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.TITLE, id="form-title")
            with ScrollableContainer():
                for field in self.FIELDS:
                    req = " *" if field.required else ""
                    lbl_cls = "field-label -required" if field.required else "field-label"
                    yield Label(f"{field.label}{req}", classes=lbl_cls)
                    default = self._initial.get(field.key, field.default) or ""
                    if field.field_type == "select":
                        yield Select(
                            options=field.options,
                            value=default or Select.BLANK,
                            id=f"field-{field.key}",
                        )
                    elif field.field_type == "switch":
                        yield Switch(value=bool(default), id=f"field-{field.key}")
                    elif field.field_type == "textarea":
                        yield TextArea(
                            str(default) if default else "",
                            id=f"field-{field.key}",
                        )
                    else:
                        pw = field.field_type == "password"
                        yield Input(
                            value=str(default) if default else "",
                            placeholder=field.placeholder,
                            password=pw,
                            id=f"field-{field.key}",
                        )
            yield Label("", id="error-label")
            with Horizontal(id="btn-row"):
                yield Button("Save", variant="primary", id="btn-save")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self._submit()
        elif event.button.id == "btn-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        data: dict[str, Any] = {}
        for field in self.FIELDS:
            widget_id = f"#field-{field.key}"
            try:
                widget = self.query_one(widget_id)
                if isinstance(widget, Input):
                    data[field.key] = widget.value
                elif isinstance(widget, Select):
                    data[field.key] = widget.value if widget.value != Select.BLANK else None
                elif isinstance(widget, Switch):
                    data[field.key] = widget.value
                elif isinstance(widget, TextArea):
                    data[field.key] = widget.text
            except Exception:
                data[field.key] = None

        error = self.validate(data)
        if error:
            try:
                self.query_one("#error-label", Label).update(f"✗ {error}")
            except Exception:
                pass
            return

        self.dismiss(data)

    def validate(self, data: dict[str, Any]) -> str | None:
        """Override to add custom validation. Return error string or None."""
        for field in self.FIELDS:
            if field.required and not data.get(field.key):
                return f"{field.label} is required."
        return None
