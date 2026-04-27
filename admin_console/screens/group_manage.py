"""GroupManagePanel — Groups + permissions shuttle + users shuttle."""

from __future__ import annotations

import uuid

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, Select, TabbedContent, TabPane

from admin_console.widgets.quick_confirm_dialog import QuickConfirmDialog
from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.form_screen import FormField, FormScreen
from admin_console.widgets.kv_panel import KVPanel
from admin_console.widgets.shuttle import ShuttleWidget


class _GroupForm(FormScreen):
    TITLE = "Group"
    FIELDS = [FormField("name", "Name", required=True)]


class GroupManagePanel(Widget):
    DEFAULT_CSS = """
    GroupManagePanel {
        height: 1fr;
        padding: 1;
    }
    GroupManagePanel #btn-row {
        height: 3;
        width: 100%;
        layout: horizontal;
    }
    GroupManagePanel #btn-row Button {
        margin-right: 1;
    }
    GroupManagePanel #main-row {
        height: 1fr;
    }
    GroupManagePanel #left-col {
        width: 40;
        height: 1fr;
    }
    GroupManagePanel #right-col {
        width: 1fr;
        height: 1fr;
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="btn-row"):
            yield Button("New", id="btn-new", variant="primary")
            yield Button("Save Changes", id="btn-save", variant="success")
            yield Button("Delete", id="btn-delete", variant="error")
            yield Button("Refresh", id="btn-refresh")
        with Horizontal(id="main-row"):
            with Vertical(id="left-col"):
                yield DataTableExt(columns=["Name", "Permissions", "Users"], id="group-table")
            with Vertical(id="right-col"):
                with TabbedContent():
                    with TabPane("Permissions", id="tab-perms"):
                        yield Select(
                            [("Global (all departments)", "")],
                            prompt="Dept scope",
                            value="",
                            id="dept-scope-select",
                        )
                        yield ShuttleWidget(
                            available_label="Available Permissions",
                            assigned_label="Assigned Permissions",
                            id="perm-shuttle",
                        )
                    with TabPane("Users", id="tab-users"):
                        yield ShuttleWidget(
                            available_label="All Users",
                            assigned_label="Group Members",
                            id="user-shuttle",
                        )

    def on_mount(self) -> None:
        self._selected_group_id: str | None = None
        self._all_permissions: list[dict] = []
        self._all_users: list[dict] = []
        self._dept_perms_with_scope: list[dict] = []
        self._all_depts: list[dict] = []
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.group_service import (  # noqa: PLC0415
            list_all_permissions,
            list_groups,
        )
        from admin_console.services.dept_service import list_depts  # noqa: PLC0415
        from admin_console.services.user_service import list_users  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        try:
            async with get_session() as db:
                groups = await list_groups(db, uuid.UUID(org_id)) if org_id else []
                perms = await list_all_permissions(db)
                users = await list_users(db, org_id=uuid.UUID(org_id) if org_id else None)
                depts = await list_depts(db, uuid.UUID(org_id)) if org_id else []

            self._groups = {g["id"]: g for g in groups}
            self._all_permissions = perms
            self._all_users = users
            self._all_depts = depts

            # Update dept scope selector options
            dept_options: list[tuple[str, str]] = [("Global (all departments)", "")] + [
                (d["name"], d["id"]) for d in depts
            ]
            self.query_one("#dept-scope-select", Select).set_options(dept_options)

            table = self.query_one("#group-table", DataTableExt)
            table.set_rows(
                [[g["name"], str(len(g.get("permissions", []))), str(len(g.get("users", [])))] for g in groups],
                [g["id"] for g in groups],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "btn-refresh": self.load_data,
            "btn-new": self._new_group,
            "btn-delete": self._delete_group,
            "btn-save": self._save_changes,
        }
        fn = actions.get(event.button.id or "")
        if fn:
            fn()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        gid = str(event.row_key.value)
        self._selected_group_id = gid
        self._load_and_populate(gid)

    @work(exclusive=True)
    async def _load_and_populate(self, group_id: str) -> None:
        """Load dept-aware permissions for the selected group, then update shuttles."""
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.group_service import get_group_permissions_with_dept  # noqa: PLC0415

        try:
            async with get_session() as db:
                self._dept_perms_with_scope = await get_group_permissions_with_dept(db, uuid.UUID(group_id))
            self._populate_shuttles(group_id)
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_select_changed(self, event: Select.Changed) -> None:
        """Re-populate the permission shuttle when the dept scope changes."""
        if event.select.id == "dept-scope-select" and self._selected_group_id:
            self._populate_shuttles(self._selected_group_id)

    def _populate_shuttles(self, group_id: str) -> None:
        # Get the selected dept scope
        scope_select = self.query_one("#dept-scope-select", Select)
        raw_dept = scope_select.value
        selected_dept: str = raw_dept if isinstance(raw_dept, str) else ""

        # Filter permissions by dept scope using dept-aware data
        if selected_dept == "":
            # Global scope: only assignments with dept_id IS NULL
            scoped_perm_ids = {p["id"] for p in self._dept_perms_with_scope if p["dept_id"] is None}
        else:
            # Dept-specific scope: only assignments for this dept
            scoped_perm_ids = {p["id"] for p in self._dept_perms_with_scope if p["dept_id"] == selected_dept}

        all_perms = getattr(self, "_all_permissions", [])
        available_perms = [(p["tag"], p["id"]) for p in all_perms if p["id"] not in scoped_perm_ids]
        assigned_perms = [(p["tag"], p["id"]) for p in all_perms if p["id"] in scoped_perm_ids]

        group = getattr(self, "_groups", {}).get(group_id, {})
        assigned_user_ids = {u["id"] for u in group.get("users", [])}
        all_users = getattr(self, "_all_users", [])
        available_users = [(u["email"], u["id"]) for u in all_users if u["id"] not in assigned_user_ids]
        assigned_users = [(u["email"], u["id"]) for u in all_users if u["id"] in assigned_user_ids]

        self.query_one("#perm-shuttle", ShuttleWidget).set_items(available_perms, assigned_perms)
        self.query_one("#user-shuttle", ShuttleWidget).set_items(available_users, assigned_users)

    @work(exclusive=True)
    async def _new_group(self) -> None:
        org_id = getattr(self.app, "active_org_id", None)
        if not org_id:
            self.notify("Select an organization first (F3)", severity="warning")
            return
        result = await self.app.push_screen_wait(_GroupForm())
        if not result:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.group_service import create_group  # noqa: PLC0415

        try:
            async with get_session() as db:
                grp = await create_group(
                    db,
                    org_id=uuid.UUID(org_id),
                    name=result["name"],
                )
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("group_create", grp["id"], {"name": grp["name"]}, org_id=org_id)
            self.notify(f"Created: {grp['name']}")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _delete_group(self) -> None:
        if not self._selected_group_id:
            self.notify("Select a group first", severity="warning")
            return
        confirmed = await self.app.push_screen_wait(QuickConfirmDialog("Delete this group?"))
        if not confirmed:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.group_service import delete_group  # noqa: PLC0415

        try:
            async with get_session() as db:
                await delete_group(db, uuid.UUID(self._selected_group_id))
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("group_delete", self._selected_group_id, {})
            self.notify("Deleted")
            self._selected_group_id = None
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _save_changes(self) -> None:
        if not self._selected_group_id:
            self.notify("Select a group first", severity="warning")
            return
        perm_ids = self.query_one("#perm-shuttle", ShuttleWidget).get_assigned()
        user_ids = self.query_one("#user-shuttle", ShuttleWidget).get_assigned()

        # Get the selected dept scope
        scope_select = self.query_one("#dept-scope-select", Select)
        raw_dept = scope_select.value
        selected_dept: str = raw_dept if isinstance(raw_dept, str) else ""
        dept_id = uuid.UUID(selected_dept) if selected_dept else None

        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.group_service import (  # noqa: PLC0415
            set_group_permissions,
            set_group_users,
        )

        try:
            gid = uuid.UUID(self._selected_group_id)
            async with get_session() as db:
                await set_group_permissions(db, gid, [uuid.UUID(p) for p in perm_ids], dept_id=dept_id)
                await set_group_users(db, gid, [uuid.UUID(u) for u in user_ids])
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("group_update", self._selected_group_id, {"perms": len(perm_ids), "users": len(user_ids)})
            self.notify("Saved")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")
