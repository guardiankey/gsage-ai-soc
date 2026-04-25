"""BackgroundTasksPanel — Tasks DataTable + result viewer."""

from __future__ import annotations

import uuid

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable

from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.json_viewer import JsonViewer


class BackgroundTasksPanel(Widget):
    DEFAULT_CSS = """
    BackgroundTasksPanel {
        height: 1fr;
        padding: 1;
        layout: horizontal;
    }
    BackgroundTasksPanel #left-col { width: 1fr; height: 1fr; }
    BackgroundTasksPanel #btn-row { height: 3; layout: horizontal; }
    BackgroundTasksPanel #btn-row Button { margin-right: 1; }
    BackgroundTasksPanel #right-col { width: 50; height: 1fr; margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="left-col"):
            with Horizontal(id="btn-row"):
                yield Button("Refresh", id="btn-refresh")
            yield DataTableExt(
                columns=["Tool", "Status", "Started", "Completed", "Profile"],
                id="task-table",
            )
        with Vertical(id="right-col"):
            yield JsonViewer(title="Result", id="task-detail")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.scheduled_service import list_background_tasks  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        try:
            async with get_session() as db:
                tasks = await list_background_tasks(db, uuid.UUID(org_id)) if org_id else []
            self._tasks = {t["id"]: t for t in tasks}
            table = self.query_one("#task-table", DataTableExt)
            table.set_rows(
                [[t["tool_name"], t.get("status", "—"), str(t.get("started_at", "—"))[:16],
                  str(t.get("completed_at", "—"))[:16], t.get("profile_id", "—")] for t in tasks],
                [t["id"] for t in tasks],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        tid = str(event.row_key.value)
        task = getattr(self, "_tasks", {}).get(tid, {})
        self.query_one("#task-detail", JsonViewer).load(task)
