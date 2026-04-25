"""DeptSelectorModal — modal to pick the active department within the current org."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label


class DeptSelectorModal(ModalScreen[str | None]):
    """Lists departments for the active org; returns dept_id string or None."""

    DEFAULT_CSS = """
    DeptSelectorModal {
        align: center middle;
    }
    DeptSelectorModal > Vertical {
        background: #2e3436;
        border: round #8ae234;
        padding: 1 2;
        width: 64;
        height: 24;
    }
    DeptSelectorModal Label {
        color: #8ae234;
        text-style: bold;
        height: 1;
        margin-bottom: 1;
    }
    DeptSelectorModal DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel", show=True),
        Binding("enter", "select_dept", "Select", show=True),
    ]

    def __init__(self, org_id: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._org_id = org_id

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Select Department  [Esc] cancel")
            yield DataTable(id="dept-table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#dept-table", DataTable)
        table.add_columns("Name", "Slug", "Default", "Active", "ID")
        self._do_load()

    def _do_load(self) -> None:
        import asyncio  # noqa: PLC0415
        asyncio.ensure_future(self._async_load())

    async def _async_load(self) -> None:
        try:
            import uuid  # noqa: PLC0415
            from admin_console.db.postgres import get_session  # noqa: PLC0415
            from admin_console.services.dept_service import list_depts  # noqa: PLC0415

            async with get_session() as db:
                depts = await list_depts(db, uuid.UUID(self._org_id))
            table = self.query_one("#dept-table", DataTable)
            table.clear()
            for dept in depts:
                table.add_row(
                    dept["name"],
                    dept["slug"],
                    "✓" if dept["is_default"] else "",
                    "✓" if dept["is_active"] else "✗",
                    dept["id"],
                    key=dept["id"],
                )
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.dismiss(str(event.row_key.value))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
