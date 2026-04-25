"""SessionsBrowsePanel — Conversations + runs drill-down + JSON detail."""

from __future__ import annotations

import uuid

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable

from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.json_viewer import JsonViewer


class SessionsBrowsePanel(Widget):
    DEFAULT_CSS = """
    SessionsBrowsePanel {
        height: 1fr;
        padding: 1;
    }
    SessionsBrowsePanel #main-row { height: 1fr; }
    SessionsBrowsePanel #sessions-col { width: 44; height: 1fr; }
    SessionsBrowsePanel #runs-col { width: 44; height: 1fr; margin-left: 1; }
    SessionsBrowsePanel #detail-col { width: 1fr; height: 1fr; margin-left: 1; }
    SessionsBrowsePanel #btn-row { height: 3; layout: horizontal; }
    SessionsBrowsePanel #btn-row Button { margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-row"):
            with Vertical(id="sessions-col"):
                with Horizontal(id="btn-row"):
                    yield Button("Refresh", id="btn-refresh")
                yield DataTableExt(
                    columns=["Session ID", "Email", "Created"],
                    id="session-table",
                )
            with Vertical(id="runs-col"):
                yield DataTableExt(
                    columns=["Run ID", "Model", "Status", "Messages"],
                    id="runs-table",
                )
            with Vertical(id="detail-col"):
                yield JsonViewer(title="Run Detail", id="run-detail")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.session_service import list_sessions  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        if not org_id:
            return
        try:
            async with get_session() as db:
                sessions = await list_sessions(db, uuid.UUID(org_id))
            self._sessions = {s["id"]: s for s in sessions}
            table = self.query_one("#session-table", DataTableExt)
            table.set_rows(
                [[s["id"][:12], s.get("user_email", "") or str(s.get("user_id", ""))[:12], s.get("created_at", "")[:16]] for s in sessions],
                [s["id"] for s in sessions],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table_id = event.data_table.parent.id if event.data_table.parent else None
        if table_id == "session-table":
            self._load_runs(str(event.row_key.value))
        elif table_id == "runs-table":
            self._show_run_detail(str(event.row_key.value))

    @work(exclusive=True)
    async def _load_runs(self, session_id: str) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.session_service import list_agent_runs  # noqa: PLC0415

        try:
            async with get_session() as db:
                runs = await list_agent_runs(db, uuid.UUID(session_id))
            self._runs = {r["id"]: r for r in runs}
            table = self.query_one("#runs-table", DataTableExt)
            table.set_rows(
                [[r["id"][:12], r.get("model", "—"), r.get("status", "—"),
                  str(r.get("message_count", "—"))] for r in runs],
                [r["id"] for r in runs],
            )
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def _show_run_detail(self, run_id: str) -> None:
        run = getattr(self, "_runs", {}).get(run_id, {})
        self.query_one("#run-detail", JsonViewer).load(run)
