"""OrgSelectorModal — modal to pick the active organization."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label


class OrgSelectorModal(ModalScreen[str | None]):
    """Lists all organizations; returns org_id string on selection or None."""

    DEFAULT_CSS = """
    OrgSelectorModal {
        align: center middle;
    }
    OrgSelectorModal > Vertical {
        background: #2e3436;
        border: round #729fcf;
        padding: 1 2;
        width: 64;
        height: 24;
    }
    OrgSelectorModal Label {
        color: #729fcf;
        text-style: bold;
        height: 1;
        margin-bottom: 1;
    }
    OrgSelectorModal DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel", show=True),
        Binding("enter", "select_org", "Select", show=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Select Organization  [Esc] cancel")
            yield DataTable(id="org-table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#org-table", DataTable)
        table.add_columns("Name", "Slug", "Active", "ID")
        self.load_orgs()

    def load_orgs(self) -> None:
        self._do_load()

    def _do_load(self) -> None:
        import asyncio  # noqa: PLC0415
        asyncio.ensure_future(self._async_load())

    async def _async_load(self) -> None:
        try:
            from admin_console.db.postgres import get_session  # noqa: PLC0415
            from admin_console.services.org_service import list_orgs  # noqa: PLC0415

            async with get_session() as db:
                orgs = await list_orgs(db)
            table = self.query_one("#org-table", DataTable)
            table.clear()
            for org in orgs:
                table.add_row(
                    org["name"],
                    org["slug"],
                    "✓" if org["is_active"] else "✗",
                    org["id"],
                    key=org["id"],
                )
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.dismiss(str(event.row_key.value))

    def action_select_org(self) -> None:
        table = self.query_one("#org-table", DataTable)
        try:
            key = table.get_cell_at(table.cursor_coordinate)
            # cursor_coordinate columns: name(0), slug(1), active(2), id(3)
            row = table.get_row_at(table.cursor_row)
            self.dismiss(str(row[-1]))
        except Exception:
            pass

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
