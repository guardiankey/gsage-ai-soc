"""DeptManagePanel — Department DataTable with create/edit/toggle active."""

from __future__ import annotations

import uuid

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable

from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.form_screen import FormField, FormScreen
from admin_console.widgets.kv_panel import KVPanel
from admin_console.widgets.quick_confirm_dialog import QuickConfirmDialog


class _DeptForm(FormScreen):
    TITLE = "Department"
    FIELDS = [
        FormField("name", "Name", required=True),
        FormField("slug", "Slug", placeholder="auto-generated if empty"),
        FormField("is_active", "Active", field_type="switch", default=True),
    ]


class DeptManagePanel(Widget):
    DEFAULT_CSS = """
    DeptManagePanel {
        height: 1fr;
        padding: 1;
        layout: horizontal;
    }
    DeptManagePanel #left-col {
        width: 1fr;
        height: 1fr;
    }
    DeptManagePanel #btn-row {
        height: 3;
        layout: horizontal;
    }
    DeptManagePanel #btn-row Button {
        margin-right: 1;
    }
    DeptManagePanel KVPanel {
        width: 44;
        height: 1fr;
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="left-col"):
            with Horizontal(id="btn-row"):
                yield Button("New", id="btn-new", variant="primary")
                yield Button("Edit", id="btn-edit")
                yield Button("Toggle Active", id="btn-toggle")
                yield Button("Delete", id="btn-delete", variant="error")
                yield Button("Refresh", id="btn-refresh")
            yield DataTableExt(
                columns=["Name", "Slug", "Active", "Default", "Org"],
                id="dept-table",
            )
        yield KVPanel(title="Detail", id="dept-detail")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.dept_service import list_depts  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        if not org_id:
            return
        try:
            async with get_session() as db:
                depts = await list_depts(db, uuid.UUID(org_id))
            self._depts = {d["id"]: d for d in depts}
            table = self.query_one("#dept-table", DataTableExt)
            table.set_rows(
                [[d["name"], d["slug"], "✓" if d["is_active"] else "✗",
                  "★" if d["is_default"] else "—", str(d.get("org_id", ""))[:8]] for d in depts],
                [d["id"] for d in depts],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()
        elif event.button.id == "btn-new":
            self._new_dept()
        elif event.button.id == "btn-edit":
            self._edit_dept()
        elif event.button.id == "btn-toggle":
            self._toggle_dept()
        elif event.button.id == "btn-delete":
            self._delete_dept()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        dept_id = str(event.row_key.value)
        dept = getattr(self, "_depts", {}).get(dept_id)
        if dept:
            self.query_one("#dept-detail", KVPanel).update(dept, title=dept["name"])

    def _selected_id(self) -> str | None:
        try:
            return self.query_one("#dept-table", DataTableExt).selected_key
        except Exception:
            return None

    @work(exclusive=True)
    async def _new_dept(self) -> None:
        result = await self.app.push_screen_wait(_DeptForm())
        if not result:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.dept_service import create_dept  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        if not org_id:
            self.notify("Select an org first (F3)", severity="warning")
            return
        try:
            async with get_session() as db:
                dept = await create_dept(
                    db,
                    org_id=uuid.UUID(org_id),
                    name=result["name"],
                    slug=result.get("slug", ""),
                    is_active=result.get("is_active", True),
                )
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("dept_create", dept["id"], {"name": dept["name"]}, org_id=org_id)
            self.notify(f"Created: {dept['name']}")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _edit_dept(self) -> None:
        dept_id = self._selected_id()
        if not dept_id:
            self.notify("Select a department first", severity="warning")
            return
        dept = getattr(self, "_depts", {}).get(dept_id, {})
        result = await self.app.push_screen_wait(_DeptForm(initial=dept))
        if not result:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.dept_service import update_dept  # noqa: PLC0415

        try:
            async with get_session() as db:
                await update_dept(
                    db,
                    uuid.UUID(dept_id),
                    name=result.get("name"),
                    slug=result.get("slug"),
                    is_active=result.get("is_active"),
                )
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("dept_update", dept_id, result)
            self.notify("Updated")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _toggle_dept(self) -> None:
        dept_id = self._selected_id()
        if not dept_id:
            self.notify("Select a department first", severity="warning")
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.dept_service import toggle_dept_active  # noqa: PLC0415

        try:
            async with get_session() as db:
                new_state = await toggle_dept_active(db, uuid.UUID(dept_id))
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("dept_toggle", dept_id, {"is_active": new_state})
            self.notify(f"is_active → {new_state}")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _delete_dept(self) -> None:
        dept_id = self._selected_id()
        if not dept_id:
            self.notify("Select a department first", severity="warning")
            return
        dept = getattr(self, "_depts", {}).get(dept_id, {})
        if dept.get("is_default"):
            self.notify("Cannot delete the default department", severity="warning")
            return
        confirmed = await self.app.push_screen_wait(
            QuickConfirmDialog(f"Delete department '{dept.get('name', dept_id)}'?")
        )
        if not confirmed:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.dept_service import delete_dept  # noqa: PLC0415

        try:
            async with get_session() as db:
                await delete_dept(db, uuid.UUID(dept_id))
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("dept_delete", dept_id, {})
            self.notify("Deleted")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")
