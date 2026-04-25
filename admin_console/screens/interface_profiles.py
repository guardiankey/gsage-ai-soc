"""InterfaceProfilesPanel — profiles list + mode toggle + permission shuttle."""

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
from admin_console.widgets.shuttle import ShuttleWidget


class _ProfileForm(FormScreen):
    TITLE = "Interface Profile"
    FIELDS = [
        FormField(
            "interface",
            "Interface",
            required=True,
            field_type="select",
            options=[
                ("Web", "web"),
                ("API", "api"),
                ("CLI", "cli"),
                ("Email", "email"),
                ("Telegram", "telegram"),
                ("WhatsApp", "whatsapp"),
                ("Slack", "slack"),
            ],
        ),
        FormField(
            "mode",
            "Mode",
            field_type="select",
            options=[("Allowlist", "allowlist"), ("Denylist", "denylist")],
            default="allowlist",
        ),
        FormField("description", "Description", placeholder="Optional description"),
    ]


class InterfaceProfilesPanel(Widget):
    DEFAULT_CSS = """
    InterfaceProfilesPanel {
        height: 1fr;
        padding: 1;
    }
    InterfaceProfilesPanel #main-row { height: 1fr; }
    InterfaceProfilesPanel #left-col { width: 50; height: 1fr; }
    InterfaceProfilesPanel #btn-row { height: 3; layout: horizontal; }
    InterfaceProfilesPanel #btn-row Button { margin-right: 1; }
    InterfaceProfilesPanel #right-col { width: 1fr; height: 1fr; margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-row"):
            with Vertical(id="left-col"):
                with Horizontal(id="btn-row"):
                    yield Button("New", id="btn-new", variant="primary")
                    yield Button("Toggle Mode", id="btn-toggle-mode")
                    yield Button("Refresh", id="btn-refresh")
                yield DataTableExt(
                    columns=["Interface", "Mode", "Active", "Description"],
                    id="profile-table",
                )
                yield KVPanel(title="Detail", id="profile-detail")
            with Vertical(id="right-col"):
                yield ShuttleWidget(
                    available_label="Available Permissions",
                    assigned_label="Assigned Permissions",
                    id="perm-shuttle",
                )
                yield Button("Save Permissions", id="btn-save", variant="primary")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.group_service import list_all_permissions  # noqa: PLC0415
        from admin_console.services.tool_service import list_interface_profiles  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        try:
            async with get_session() as db:
                profiles = await list_interface_profiles(db, uuid.UUID(org_id)) if org_id else []
                perms = await list_all_permissions(db)
            self._profiles = {p["id"]: p for p in profiles}
            self._all_perms = perms
            table = self.query_one("#profile-table", DataTableExt)
            table.set_rows(
                [[p["interface"], p.get("mode", "—"), "✓" if p.get("is_active") else "✗",
                  p.get("description", "")[:40]] for p in profiles],
                [p["id"] for p in profiles],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        pid = str(event.row_key.value)
        self._selected_profile_id = pid
        profile = getattr(self, "_profiles", {}).get(pid, {})
        if profile:
            self.query_one("#profile-detail", KVPanel).update(
                {k: v for k, v in profile.items() if k not in ("tool_permissions",)}
            )
            assigned_ids = {p["id"] for p in profile.get("tool_permissions", [])}
            all_perms = getattr(self, "_all_perms", [])
            avail = [(p["tag"], p["id"]) for p in all_perms if p["id"] not in assigned_ids]
            assigned = [(p["tag"], p["id"]) for p in all_perms if p["id"] in assigned_ids]
            self.query_one("#perm-shuttle", ShuttleWidget).set_items(avail, assigned)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()
        elif event.button.id == "btn-new":
            self._new_profile()
        elif event.button.id == "btn-toggle-mode":
            self._toggle_mode()
        elif event.button.id == "btn-save":
            self._save_permissions()

    @work(exclusive=True)
    async def _new_profile(self) -> None:
        result = await self.app.push_screen_wait(_ProfileForm())
        if not result:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.tool_service import create_interface_profile  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        if not org_id:
            self.notify("Select an org first (F3)", severity="warning")
            return
        try:
            async with get_session() as db:
                profile = await create_interface_profile(
                    db,
                    org_id=uuid.UUID(org_id),
                    interface=result.get("interface", "api"),
                    mode=result.get("mode", "allowlist"),
                    description=result.get("description", ""),
                )
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("profile_create", profile["id"], {"interface": profile["interface"]}, org_id=org_id)
            self.notify(f"Created: {profile['interface']}")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _toggle_mode(self) -> None:
        pid = getattr(self, "_selected_profile_id", None)
        if not pid:
            self.notify("Select a profile first", severity="warning")
            return
        profile = getattr(self, "_profiles", {}).get(pid, {})
        current = profile.get("mode", "allowlist")
        new_mode = "denylist" if current == "allowlist" else "allowlist"
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.tool_service import update_interface_profile  # noqa: PLC0415

        try:
            async with get_session() as db:
                await update_interface_profile(db, uuid.UUID(pid), mode=new_mode)
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("profile_mode_toggle", pid, {"mode": new_mode})
            self.notify(f"Mode → {new_mode}")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _save_permissions(self) -> None:
        pid = getattr(self, "_selected_profile_id", None)
        if not pid:
            self.notify("Select a profile first", severity="warning")
            return
        perm_ids = self.query_one("#perm-shuttle", ShuttleWidget).get_assigned()
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.tool_service import update_interface_profile  # noqa: PLC0415

        try:
            async with get_session() as db:
                await update_interface_profile(db, uuid.UUID(pid), permission_ids=perm_ids)
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("profile_perms_update", pid, {"count": len(perm_ids)})
            self.notify("Saved")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")
