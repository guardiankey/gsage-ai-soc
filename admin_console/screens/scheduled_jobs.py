"""ScheduledJobsPanel — Jobs DataTable + toggle + RedBeat status."""

from __future__ import annotations

import uuid

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, Label

from admin_console.widgets.quick_confirm_dialog import QuickConfirmDialog
from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.kv_panel import KVPanel


class ScheduledJobsPanel(Widget):
    DEFAULT_CSS = """
    ScheduledJobsPanel {
        height: 1fr;
        padding: 1;
    }
    ScheduledJobsPanel #top-row { height: 3; layout: horizontal; }
    ScheduledJobsPanel #top-row Button { margin-right: 1; }
    ScheduledJobsPanel #main-row { height: 1fr; }
    ScheduledJobsPanel #jobs-col { width: 1fr; height: 1fr; }
    ScheduledJobsPanel #redbeat-col { width: 44; height: 1fr; margin-left: 1; }
    ScheduledJobsPanel KVPanel { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-row"):
            yield Button("Toggle Active", id="btn-toggle")
            yield Button("Refresh", id="btn-refresh")
            yield Label("", id="status-label")
        with Horizontal(id="main-row"):
            with Vertical(id="jobs-col"):
                yield DataTableExt(
                    columns=["Job Name", "Cron", "Active", "Last Run", "Type"],
                    id="jobs-table",
                )
            with Vertical(id="redbeat-col"):
                yield KVPanel(title="RedBeat Keys", id="redbeat-panel")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        import asyncio  # noqa: PLC0415

        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.db.redis_client import redbeat_keys  # noqa: PLC0415
        from admin_console.services.scheduled_service import list_jobs  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        try:
            async with get_session() as db:
                jobs = await list_jobs(db, uuid.UUID(org_id)) if org_id else []
            self._jobs = {j["id"]: j for j in jobs}
            table = self.query_one("#jobs-table", DataTableExt)
            table.set_rows(
                [[j["name"], j.get("cron_expression", "—"), "✓" if j.get("is_active") else "✗",
                  str(j.get("last_run_at", "—"))[:16], j.get("job_type", "—")] for j in jobs],
                [j["id"] for j in jobs],
            )

            keys = await asyncio.to_thread(redbeat_keys)
            kv_data = {k: "" for k in keys[:50]}
            self.query_one("#redbeat-panel", KVPanel).update(kv_data, title=f"RedBeat ({len(keys)} keys)")
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._selected_job_id = str(event.row_key.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()
        elif event.button.id == "btn-toggle":
            self._toggle_job()

    @work(exclusive=True)
    async def _toggle_job(self) -> None:
        jid = getattr(self, "_selected_job_id", None)
        if not jid:
            self.notify("Select a job first", severity="warning")
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.scheduled_service import toggle_job_active  # noqa: PLC0415

        try:
            async with get_session() as db:
                state = await toggle_job_active(db, uuid.UUID(jid))
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("job_toggle", jid, {"is_active": state})
            self.notify(f"is_active → {state}")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")
