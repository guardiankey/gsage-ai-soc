"""QuickConfirmDialog — simple Yes/No modal (no text input required)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class QuickConfirmDialog(ModalScreen[bool]):
    """Ask the user to confirm an action with a simple Yes/Cancel dialog.

    Use this for non-critical destructive actions (delete, revoke, reset).
    For truly destructive operations (flush DB, truncate tables, delete indices)
    use :class:`ConfirmDialog` which requires typing "CONFIRM".

    Usage::

        confirmed = await self.app.push_screen_wait(QuickConfirmDialog("Delete this item?"))
        if confirmed:
            ...
    """

    DEFAULT_CSS = """
    QuickConfirmDialog {
        align: center middle;
    }
    QuickConfirmDialog > Vertical {
        background: #2e3436;
        border: round #c4a000;
        padding: 1 2;
        width: 52;
        height: auto;
    }
    QuickConfirmDialog Label#quick-msg {
        color: #c4a000;
        text-style: bold;
        margin-bottom: 1;
    }
    QuickConfirmDialog #btn-row {
        layout: horizontal;
        height: auto;
        align: right middle;
    }
    QuickConfirmDialog #btn-row Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_false", "Cancel", show=False),
        Binding("enter", "confirm_yes", "Confirm", show=False),
    ]

    def __init__(self, message: str = "Are you sure?") -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"⚠  {self._message}", id="quick-msg")
            with Center(id="btn-row"):
                yield Button("Yes", variant="warning", id="btn-yes")
                yield Button("Cancel", variant="default", id="btn-no")

    def on_mount(self) -> None:
        self.query_one("#btn-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")

    def action_dismiss_false(self) -> None:
        self.dismiss(False)

    def action_confirm_yes(self) -> None:
        self.dismiss(True)
