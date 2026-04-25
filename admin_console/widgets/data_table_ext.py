"""DataTableExt — DataTable with an inline search filter."""

from __future__ import annotations

from typing import Any, Callable

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import DataTable, Input


class DataTableExt(Widget):
    """A :class:`DataTable` with an :class:`Input` filter above it.

    Usage::

        table = DataTableExt(columns=["Name", "Status", "ID"])
        await table.set_rows(rows)          # list[tuple] or list[list]
        await table.set_rows(rows, keys)    # with optional row keys

        # Get selected row data
        row = table.selected_row
    """

    DEFAULT_CSS = """
    DataTableExt {
        height: 1fr;
    }
    DataTableExt #search-input {
        height: 3;
        margin-bottom: 0;
        border: round #555753;
    }
    DataTableExt DataTable {
        height: 1fr;
    }
    """

    filter_value: reactive[str] = reactive("", init=False)

    def __init__(
        self,
        columns: list[str] | None = None,
        placeholder: str = "Filter…",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._columns = columns or []
        self._placeholder = placeholder
        self._all_rows: list[tuple[list[Any], str | None]] = []

    def compose(self) -> ComposeResult:
        yield Input(placeholder=self._placeholder, id="search-input")
        yield DataTable(id="inner-table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#inner-table", DataTable)
        if self._columns:
            table.add_columns(*self._columns)

    def on_input_changed(self, event: Input.Changed) -> None:
        self.filter_value = event.value

    def watch_filter_value(self, value: str) -> None:
        self._apply_filter(value)

    def set_columns(self, columns: list[str]) -> None:
        self._columns = columns
        table = self.query_one("#inner-table", DataTable)
        table.clear(columns=True)
        table.add_columns(*columns)

    def set_rows(
        self,
        rows: list[list[Any]],
        keys: list[str | None] | None = None,
    ) -> None:
        """Load all rows; apply current filter immediately."""
        self._all_rows = [
            (row, (keys[i] if keys and i < len(keys) else None))
            for i, row in enumerate(rows)
        ]
        self._apply_filter(self.filter_value)

    def clear(self) -> None:
        self._all_rows = []
        table = self.query_one("#inner-table", DataTable)
        table.clear()

    @property
    def selected_row(self) -> list[Any] | None:
        """Return the data of the currently highlighted row."""
        table = self.query_one("#inner-table", DataTable)
        if table.cursor_row < 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key
            for row, key in self._all_rows:
                if key == row_key.value:
                    return row
            # Fallback: return by index
            visible = self._visible_rows()
            if 0 <= table.cursor_row < len(visible):
                return visible[table.cursor_row][0]
        except Exception:
            pass
        return None

    @property
    def selected_key(self) -> str | None:
        """Return the row key of the currently highlighted row."""
        table = self.query_one("#inner-table", DataTable)
        if table.cursor_row < 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key
            return str(row_key.value) if row_key.value is not None else None
        except Exception:
            return None

    def _visible_rows(self) -> list[tuple[list[Any], str | None]]:
        q = self.filter_value.lower()
        if not q:
            return self._all_rows
        return [
            (row, key)
            for row, key in self._all_rows
            if any(q in str(cell).lower() for cell in row)
        ]

    def _apply_filter(self, query: str) -> None:
        table = self.query_one("#inner-table", DataTable)
        table.clear()
        for row, key in self._visible_rows():
            table.add_row(*row, key=key)
