"""KnowledgeBrowsePanel — KB entries + search + delete + ingest jobs."""

from __future__ import annotations

import uuid

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, TabbedContent, TabPane

from admin_console.widgets.quick_confirm_dialog import QuickConfirmDialog
from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.json_viewer import JsonViewer


class KnowledgeBrowsePanel(Widget):
    DEFAULT_CSS = """
    KnowledgeBrowsePanel {
        height: 1fr;
        padding: 1;
    }
    KnowledgeBrowsePanel #top-row { height: 3; layout: horizontal; }
    KnowledgeBrowsePanel #top-row Button { margin-right: 1; }
    KnowledgeBrowsePanel #search-input { width: 1fr; }
    KnowledgeBrowsePanel #main-row { height: 1fr; }
    KnowledgeBrowsePanel #left-col { width: 1fr; height: 1fr; }
    KnowledgeBrowsePanel #right-col { width: 50; height: 1fr; margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-row"):
            yield Input(placeholder="Search knowledge base…", id="search-input")
            yield Button("Search", id="btn-search", variant="primary")
            yield Button("Delete Entry", id="btn-delete", variant="error")
            yield Button("Refresh", id="btn-refresh")
        with Horizontal(id="main-row"):
            with Vertical(id="left-col"):
                with TabbedContent():
                    with TabPane("Search Results / Entries"):
                        yield DataTableExt(
                            columns=["UUID", "Content Preview", "Score"],
                            id="kb-table",
                        )
                    with TabPane("Ingest Jobs"):
                        yield DataTableExt(
                            columns=["Filename", "Status", "Chunks", "Created"],
                            id="jobs-table",
                        )
            with Vertical(id="right-col"):
                yield JsonViewer(title="Entry Detail", id="entry-detail")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.knowledge_ops import list_ingest_jobs  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        if not org_id:
            return
        try:
            async with get_session() as db:
                jobs = await list_ingest_jobs(db, uuid.UUID(org_id))
            table = self.query_one("#jobs-table", DataTableExt)
            table.set_rows(
                [[j["original_filename"], j["status"], str(j.get("chunks_stored", 0)), j["created_at"][:16]] for j in jobs],
                [j["id"] for j in jobs],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()
        elif event.button.id == "btn-search":
            self._search_kb()
        elif event.button.id == "btn-delete":
            self._delete_entry()

    @work(exclusive=True)
    async def _search_kb(self) -> None:
        query = self.query_one("#search-input", Input).value.strip()
        if not query:
            return
        org_id = getattr(self.app, "active_org_id", None)
        if not org_id:
            self.notify("Select an org first", severity="warning")
            return
        from admin_console.services.knowledge_ops import search_kb  # noqa: PLC0415

        try:
            results = await search_kb(org_id, query)
            self._kb_results = {r.get("_additional", {}).get("id", str(i)): r for i, r in enumerate(results)}
            table = self.query_one("#kb-table", DataTableExt)
            table.set_rows(
                [[r.get("_additional", {}).get("id", "—")[:12],
                  str(r.get("content", r.get("text", "")))[:60],
                  str(r.get("_additional", {}).get("score", "—"))] for r in results],
                [r.get("_additional", {}).get("id", str(i)) for i, r in enumerate(results)],
            )
        except Exception as exc:
            self.notify(f"Search error: {exc}", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.parent and event.data_table.parent.id == "kb-table":
            uid = str(event.row_key.value)
            entry = getattr(self, "_kb_results", {}).get(uid, {})
            self.query_one("#entry-detail", JsonViewer).load(entry, title=f"Entry {uid[:8]}")

    @work(exclusive=True)
    async def _delete_entry(self) -> None:
        dt = self.query_one("#kb-table DataTable", DataTable)
        try:
            rk = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
            uid = str(rk.value)
        except Exception:
            self.notify("Select an entry first", severity="warning")
            return

        confirmed = await self.app.push_screen_wait(QuickConfirmDialog(f"Delete KB entry {uid[:12]}?"))
        if not confirmed:
            return
        from admin_console.services.knowledge_ops import delete_kb_object  # noqa: PLC0415

        ok, err = await delete_kb_object(uid)
        if ok:
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("kb_delete", uid, {})
            self.notify("Deleted")
            self._search_kb()
        else:
            self.notify(f"Error: {err}", severity="error")
