"""FilesBrowsePanel — Tab1: DB table, Tab2: MinIO bucket browser."""

from __future__ import annotations

import uuid

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, TabbedContent, TabPane, Input

from admin_console.widgets.quick_confirm_dialog import QuickConfirmDialog
from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.kv_panel import KVPanel


class FilesBrowsePanel(Widget):
    DEFAULT_CSS = """
    FilesBrowsePanel {
        height: 1fr;
        padding: 1;
    }
    FilesBrowsePanel #btn-row { height: 3; layout: horizontal; }
    FilesBrowsePanel #btn-row Button { margin-right: 1; }
    FilesBrowsePanel #bucket-row { height: 3; layout: horizontal; }
    FilesBrowsePanel #bucket-input { width: 1fr; }
    FilesBrowsePanel #main { height: 1fr; }
    FilesBrowsePanel KVPanel { height: 12; }
    """

    def compose(self) -> ComposeResult:
        with TabbedContent(id="main"):
            with TabPane("DB Files"):
                with Horizontal(id="btn-row"):
                    yield Button("Delete Record", id="btn-delete-db", variant="error")
                    yield Button("Refresh", id="btn-refresh-db")
                yield DataTableExt(
                    columns=["Filename", "Tool", "Size", "Purged", "Expires", "Created"],
                    id="db-files-table",
                )
                yield KVPanel(title="File Detail", id="file-detail")
            with TabPane("MinIO Browser"):
                with Horizontal(id="bucket-row"):
                    yield Input(placeholder="bucket name", id="bucket-input", value="gsage")
                    yield Input(placeholder="prefix (optional)", id="prefix-input")
                    yield Button("Browse", id="btn-browse", variant="primary")
                    yield Button("Copy URL", id="btn-url")
                yield DataTableExt(
                    columns=["Object Key", "Size", "Last Modified"],
                    id="minio-table",
                )

    def on_mount(self) -> None:
        self.load_db_files()

    @work(exclusive=True)
    async def load_db_files(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.file_ops import list_files_db  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        if not org_id:
            return
        try:
            async with get_session() as db:
                files = await list_files_db(db, uuid.UUID(org_id))
            self._files = {f["id"]: f for f in files}
            table = self.query_one("#db-files-table", DataTableExt)
            table.set_rows(
                [[f["filename"], f["tool_name"], f"{f['size_bytes'] // 1024}KB",
                  "✓" if f.get("purged_at") else "—",
                  f["expires_at"][:10] if f.get("expires_at") else "never",
                  f["created_at"][:10]] for f in files],
                [f["id"] for f in files],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh-db":
            self.load_db_files()
        elif event.button.id == "btn-delete-db":
            self._delete_db_file()
        elif event.button.id == "btn-browse":
            self._browse_minio()
        elif event.button.id == "btn-url":
            self._copy_presigned_url()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.parent and event.data_table.parent.id == "db-files-table":
            fid = str(event.row_key.value)
            f = getattr(self, "_files", {}).get(fid, {})
            self.query_one("#file-detail", KVPanel).update(f)

    @work(exclusive=True)
    async def _delete_db_file(self) -> None:
        try:
            dt = self.query_one("#db-files-table DataTable", DataTable)
            rk = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
            fid = str(rk.value)
        except Exception:
            self.notify("Select a file first", severity="warning")
            return
        confirmed = await self.app.push_screen_wait(QuickConfirmDialog("Delete this file record?"))
        if not confirmed:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.file_ops import purge_file_record  # noqa: PLC0415

        try:
            async with get_session() as db:
                await purge_file_record(db, uuid.UUID(fid))
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("file_delete", fid, {})
            self.notify("Deleted")
            self.load_db_files()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(thread=True)
    def _browse_minio(self) -> None:
        bucket = self.query_one("#bucket-input", Input).value.strip() or "gsage"
        prefix = self.query_one("#prefix-input", Input).value.strip()
        from admin_console.services.file_ops import list_files_minio  # noqa: PLC0415

        try:
            objects = list_files_minio(bucket, prefix)
            self._minio_objects = objects

            def _update():
                table = self.query_one("#minio-table", DataTableExt)
                table.set_rows(
                    [[o.get("key", ""), f"{o.get('size', 0) // 1024}KB", o.get("last_modified", "")[:16]] for o in objects],
                    [o.get("key", str(i)) for i, o in enumerate(objects)],
                )

            self.app.call_from_thread(_update)
        except Exception as exc:
            self.app.call_from_thread(lambda: self.notify(str(exc), severity="error"))

    def _copy_presigned_url(self) -> None:
        try:
            dt = self.query_one("#minio-table DataTable", DataTable)
            rk = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
            key = str(rk.value)
            bucket = self.query_one("#bucket-input", Input).value.strip() or "gsage"
            from admin_console.services.file_ops import get_presigned_url  # noqa: PLC0415
            url = get_presigned_url(bucket, key)
            self.notify(f"URL: {url[:80]}…", timeout=10)
        except Exception as exc:
            self.notify(str(exc), severity="error")
