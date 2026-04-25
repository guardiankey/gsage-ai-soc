"""ConfirmDialog — modal that requires the user to type CONFIRM."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class ConfirmDialog(ModalScreen[bool]):
    """Ask the user to type "CONFIRM" before a destructive action.

    Usage::

        result = await self.app.push_screen_wait(ConfirmDialog("Delete all sessions?"))
        if result:
            ...
    """

    DEFAULT_CSS = """
    ConfirmDialog {
        align: center middle;
    }
    ConfirmDialog > Vertical {
        background: #2e3436;
        border: round #ef2929;
        padding: 1 2;
        width: 52;
        height: auto;
    }
    ConfirmDialog Label#confirm-msg {
        color: #ef2929;
        text-style: bold;
        margin-bottom: 1;
    }
    ConfirmDialog Label#confirm-hint {
        color: #babdb6;
        margin-bottom: 1;
    }
    ConfirmDialog Input {
        margin-bottom: 1;
    }
    ConfirmDialog #btn-row {
        layout: horizontal;
        height: auto;
        align: right middle;
    }
    ConfirmDialog #btn-row Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_false", "Cancel", show=False),
    ]

    def __init__(self, message: str = "Are you sure?") -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"⚠  {self._message}", id="confirm-msg")
            yield Label('Type "CONFIRM" to proceed:', id="confirm-hint")
            yield Input(placeholder="CONFIRM", id="confirm-input")
            with Center(id="btn-row"):
                yield Button("Confirm", variant="error", id="btn-yes")
                yield Button("Cancel", variant="default", id="btn-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-yes":
            value = self.query_one("#confirm-input", Input).value
            self.dismiss(value.strip() == "CONFIRM")
        else:
            self.dismiss(False)

    def action_dismiss_false(self) -> None:
        self.dismiss(False)
