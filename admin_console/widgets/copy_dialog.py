"""CopyDialog — persistent modal to display and copy a sensitive value."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class CopyDialog(ModalScreen[None]):
    """Display a sensitive value (password, API key) in a persistent dialog.

    Unlike a notification toast, this dialog remains open until explicitly
    closed so the user has time to copy the value.

    The Input widget is pre-focused so the user can immediately Ctrl+A / Ctrl+C.

    Usage::

        await self.app.push_screen_wait(CopyDialog("New password", new_pw))
    """

    DEFAULT_CSS = """
    CopyDialog {
        align: center middle;
    }
    CopyDialog > Vertical {
        background: #2e3436;
        border: round #4e9a06;
        padding: 1 2;
        width: 64;
        height: auto;
    }
    CopyDialog Label#copy-title {
        color: #4e9a06;
        text-style: bold;
        margin-bottom: 1;
    }
    CopyDialog Label#copy-hint {
        color: #888a85;
        margin-bottom: 1;
    }
    CopyDialog Input {
        margin-bottom: 1;
    }
    CopyDialog #btn-close {
        width: 100%;
    }
    """

    BINDINGS = [
        Binding("escape", "action_dismiss", "Close", show=False),
    ]

    def __init__(self, title: str, value: str) -> None:
        super().__init__()
        self._title = title
        self._value = value

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title, id="copy-title")
            yield Label("Copy the value below before closing:", id="copy-hint")
            yield Input(value=self._value, id="copy-input")
            yield Button("Close", id="btn-close", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#copy-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close":
            self.dismiss(None)

    def action_dismiss(self, result: None = None) -> None:  # type: ignore[override]
        self.dismiss(None)
