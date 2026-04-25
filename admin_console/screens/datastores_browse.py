"""DatastoresBrowsePanel — Left: store list, Right: record browser, schema viewer."""

from __future__ import annotations

import uuid

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, TabbedContent, TabPane

from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.json_viewer import JsonViewer
from admin_console.widgets.kv_panel import KVPanel


class DatastoresBrowsePanel(Widget):
    DEFAULT_CSS = """
    DatastoresBrowsePanel {
        height: 1fr;
        padding: 1;
        layout: horizontal;
    }
    DatastoresBrowsePanel #stores-col { width: 40; height: 1fr; }
    DatastoresBrowsePanel #btn-row { height: 3; layout: horizontal; }
    DatastoresBrowsePanel #btn-row Button { margin-right: 1; }
    DatastoresBrowsePanel #right-col { width: 1fr; height: 1fr; margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="stores-col"):
            with Horizontal(id="btn-row"):
                yield Button("Refresh", id="btn-refresh")
            yield DataTableExt(columns=["Name", "Type", "Records", "Org"], id="stores-table")
        with Vertical(id="right-col"):
            with TabbedContent():
                with TabPane("Records"):
                    yield DataTableExt(columns=["Data Preview", "Created"], id="records-table")
                with TabPane("Schema"):
                    yield JsonViewer(title="Schema", id="schema-viewer")
            yield KVPanel(title="Store Info", id="store-info")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from sqlalchemy import func, select  # noqa: PLC0415

        from src.shared.models.datastore import GSageDataStore, GSageDataStoreRecord  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        try:
            async with get_session() as db:
                q = select(GSageDataStore)
                if org_id:
                    q = q.where(GSageDataStore.org_id == uuid.UUID(org_id))
                result = await db.execute(q.order_by(GSageDataStore.name))
                stores = result.scalars().all()

                # Count records per store
                counts = {}
                for store in stores:
                    cnt = await db.execute(
                        select(func.count()).where(
                            GSageDataStoreRecord.datastore_id == store.id
                        )
                    )
                    counts[str(store.id)] = cnt.scalar() or 0

            self._stores = {str(s.id): s for s in stores}
            self._counts = counts
            table = self.query_one("#stores-table", DataTableExt)
            table.set_rows(
                [[s.name, s.visibility or "—", str(counts.get(str(s.id), 0)),
                  str(s.org_id)[:8]] for s in stores],
                [str(s.id) for s in stores],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.parent and event.data_table.parent.id == "stores-table":
            sid = str(event.row_key.value)
            self._load_records(sid)

    @work(exclusive=True)
    async def _load_records(self, store_id: str) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from sqlalchemy import select  # noqa: PLC0415

        from src.shared.models.datastore import GSageDataStore, GSageDataStoreRecord  # noqa: PLC0415

        try:
            async with get_session() as db:
                store = self._stores.get(store_id)
                if store:
                    self.query_one("#store-info", KVPanel).update({
                        "id": str(store.id),
                        "name": store.name,
                        "visibility": store.visibility or "—",
                        "org_id": str(store.org_id),
                    })
                    schema = getattr(store, "schema", None) or {}
                    self.query_one("#schema-viewer", JsonViewer).load(schema or {})

                result = await db.execute(
                    select(GSageDataStoreRecord)
                    .where(GSageDataStoreRecord.datastore_id == uuid.UUID(store_id))
                    .order_by(GSageDataStoreRecord.created_at.desc())
                    .limit(100)
                )
                records = result.scalars().all()

            table = self.query_one("#records-table", DataTableExt)
            table.set_rows(
                [[str(r.data)[:60], str(r.created_at)[:16]] for r in records],
                [str(r.id) for r in records],
            )
        except Exception as exc:
            self.notify(str(exc), severity="error")
