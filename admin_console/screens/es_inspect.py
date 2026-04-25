"""EsInspectPanel — health card + indices + templates + trace search."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, TabbedContent, TabPane

from admin_console.widgets.confirm_dialog import ConfirmDialog
from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.json_viewer import JsonViewer
from admin_console.widgets.kv_panel import KVPanel


class EsInspectPanel(Widget):
    DEFAULT_CSS = """
    EsInspectPanel {
        height: 1fr;
        padding: 1;
    }
    EsInspectPanel #top-row { height: 3; layout: horizontal; }
    EsInspectPanel #top-row Button { margin-right: 1; }
    EsInspectPanel #search-row { height: 3; layout: horizontal; }
    EsInspectPanel #search-input { width: 1fr; }
    EsInspectPanel #index-input { width: 20; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-row"):
            yield Button("Refresh", id="btn-refresh")
            yield Button("Delete Index", id="btn-delete-index", variant="error")
        with TabbedContent():
            with TabPane("Health"):
                yield KVPanel(title="Cluster Health", id="health-panel")
            with TabPane("Indices"):
                yield DataTableExt(
                    columns=["Index", "Docs", "Size", "Status"],
                    id="indices-table",
                )
            with TabPane("Templates"):
                yield DataTableExt(columns=["Template"], id="templates-table")
            with TabPane("Trace Search"):
                with Horizontal(id="search-row"):
                    yield Input(placeholder="Trace query…", id="search-input")
                    yield Input(placeholder="index", value="gsage-traces*", id="index-input")
                    yield Button("Search", id="btn-search-trace", variant="primary")
                yield JsonViewer(title="Hits", id="trace-results")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        import asyncio  # noqa: PLC0415

        from admin_console.db.es_client import (  # noqa: PLC0415
            es_health,
            es_index_templates,
            es_indices,
        )

        try:
            health = await asyncio.to_thread(es_health)
            indices = await asyncio.to_thread(es_indices)
            templates = await asyncio.to_thread(es_index_templates)

            self.query_one("#health-panel", KVPanel).update(health)

            idx_table = self.query_one("#indices-table", DataTableExt)
            idx_table.set_rows(
                [[i.get("index", ""), i.get("docs.count", ""), i.get("store.size", ""),
                  i.get("health", "—")] for i in indices],
                [i.get("index", str(n)) for n, i in enumerate(indices)],
            )

            tmpl_table = self.query_one("#templates-table", DataTableExt)
            tmpl_keys: list[str | None] = list(templates) if templates else []
            tmpl_table.set_rows(
                [[t] for t in templates],
                tmpl_keys,
            )
            self._indices = [i.get("index", "") for i in indices]
        except Exception as exc:
            self.notify(f"ES error: {exc}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()
        elif event.button.id == "btn-search-trace":
            self._search_traces()
        elif event.button.id == "btn-delete-index":
            self._delete_index()

    @work(exclusive=True)
    async def _search_traces(self) -> None:
        import asyncio  # noqa: PLC0415

        from admin_console.db.es_client import es_search_traces  # noqa: PLC0415

        query = self.query_one("#search-input", Input).value.strip()
        index = self.query_one("#index-input", Input).value.strip() or "gsage-traces*"
        try:
            results = await asyncio.to_thread(es_search_traces, index, query, 20)
            self.query_one("#trace-results", JsonViewer).load(results, title=f"Hits in {index}")
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _delete_index(self) -> None:
        table = self.query_one("#indices-table DataTable", DataTable)
        try:
            rk = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            index_name = str(rk.value)
        except Exception:
            self.notify("Select an index first", severity="warning")
            return
        confirmed = await self.app.push_screen_wait(ConfirmDialog(f"Delete index '{index_name}'?"))
        if not confirmed:
            return
        import asyncio  # noqa: PLC0415

        from admin_console.db.es_client import es_delete_index  # noqa: PLC0415

        ok, err = await asyncio.to_thread(es_delete_index, index_name)
        if ok:
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("es_delete_index", index_name, {})
            self.notify(f"Deleted {index_name}")
            self.load_data()
        else:
            self.notify(f"Error: {err}", severity="error")
