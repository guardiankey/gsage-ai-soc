"""KVPanel — key-value detail panel."""

from __future__ import annotations

from rich.table import Table
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static


class KVPanel(Widget):
    """Renders a key-value table with optional masking of secret values.

    Usage::

        yield KVPanel(
            {"id": "abc", "name": "acme", "secret": "****"},
            title="Organization Detail",
        )
        panel.update({"id": "def", ...})
    """

    DEFAULT_CSS = """
    KVPanel {
        height: auto;
        border: round #555753;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    def __init__(
        self,
        data: dict | None = None,
        title: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._data: dict = data or {}
        self._title = title

    def compose(self) -> ComposeResult:
        yield Static(self._render_table(), id="kv-static")

    def update(self, data: dict, title: str | None = None) -> None:
        self._data = data
        if title is not None:
            self._title = title
        try:
            self.query_one("#kv-static", Static).update(self._render_table())
        except Exception:
            pass

    def clear(self) -> None:
        self._data = {}
        try:
            self.query_one("#kv-static", Static).update("")
        except Exception:
            pass

    def _render_table(self) -> Table:
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Key", style="bold #729fcf", min_width=20)
        table.add_column("Value", style="#eeeeec")
        if self._title:
            table.title = f"[bold #8ae234]{self._title}[/]"
        for key, value in self._data.items():
            table.add_row(str(key), str(value) if value is not None else "—")
        return table
