"""InterfaceProfilesPanel — profiles list + mode toggle + permission shuttle."""

from __future__ import annotations

import json
import uuid
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable

from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.form_screen import FormField, FormScreen
from admin_console.widgets.kv_panel import KVPanel
from admin_console.widgets.quick_confirm_dialog import QuickConfirmDialog
from admin_console.widgets.shuttle import ShuttleWidget


class _ProfileForm(FormScreen):
    TITLE = "Interface Profile"

    def __init__(
        self,
        initial: dict[str, Any] | None = None,
        *,
        dept_options: list[tuple[str, Any]] | None = None,
    ) -> None:
        # Build FIELDS dynamically so dept options reflect the active org.
        # Pre-format JSON fields if initial values are dicts.
        prepared: dict[str, Any] = dict(initial or {})
        for k in ("interface_config", "preferences"):
            v = prepared.get(k)
            if isinstance(v, dict):
                prepared[k] = json.dumps(v, indent=2, ensure_ascii=False) if v else ""
            elif v is None:
                prepared[k] = ""

        dept_opts: list[tuple[str, Any]] = [("Org-wide (no dept)", "")] + list(dept_options or [])

        self.FIELDS = [
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
                "dept_id",
                "Department",
                field_type="select",
                options=dept_opts,
                default="",
            ),
            FormField(
                "mode",
                "Mode",
                field_type="select",
                options=[("Allowlist", "allowlist"), ("Denylist", "denylist")],
                default="allowlist",
            ),
            FormField("description", "Description", placeholder="Optional description"),
            FormField("system_prompt", "System Prompt", field_type="textarea"),
            FormField("interface_config", "Interface Config (JSON)", field_type="textarea"),
            FormField("preferences", "Preferences (JSON)", field_type="textarea"),
        ]
        super().__init__(initial=prepared)

    def validate(self, data: dict[str, Any]) -> str | None:
        base = super().validate(data)
        if base:
            return base
        for k in ("interface_config", "preferences"):
            raw = (data.get(k) or "").strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        return f"{k} must be a JSON object"
                except json.JSONDecodeError as exc:
                    return f"{k}: invalid JSON ({exc})"
        return None


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
                    yield Button("Edit", id="btn-edit")
                    yield Button("Delete", id="btn-delete", variant="error")
                    yield Button("Toggle Mode", id="btn-toggle-mode")
                    yield Button("Refresh", id="btn-refresh")
                yield DataTableExt(
                    columns=["Interface", "Dept", "Mode", "Active", "Description"],
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
        from admin_console.services.dept_service import list_depts  # noqa: PLC0415
        from admin_console.services.group_service import list_all_permissions  # noqa: PLC0415
        from admin_console.services.tool_service import list_interface_profiles  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        try:
            async with get_session() as db:
                profiles = await list_interface_profiles(db, uuid.UUID(org_id)) if org_id else []
                perms = await list_all_permissions(db)
                depts = await list_depts(db, uuid.UUID(org_id)) if org_id else []
            self._profiles = {p["id"]: p for p in profiles}
            self._all_perms = perms
            self._depts = depts
            self._dept_name_by_id = {d["id"]: d["name"] for d in depts}
            table = self.query_one("#profile-table", DataTableExt)
            table.set_rows(
                [[
                    p["interface"],
                    self._dept_name_by_id.get(p.get("dept_id") or "", "—") if p.get("dept_id") else "Org-wide",
                    p.get("mode", "—"),
                    "✓" if p.get("is_active") else "✗",
                    (p.get("description") or "")[:40],
                ] for p in profiles],
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
            assigned_tags = set(profile.get("tool_permissions") or [])
            all_perms = getattr(self, "_all_perms", [])
            avail = [(p["tag"], p["tag"]) for p in all_perms if p["tag"] not in assigned_tags]
            assigned = [(p["tag"], p["tag"]) for p in all_perms if p["tag"] in assigned_tags]
            self.query_one("#perm-shuttle", ShuttleWidget).set_items(avail, assigned)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()
        elif event.button.id == "btn-new":
            self._new_profile()
        elif event.button.id == "btn-edit":
            self._edit_profile()
        elif event.button.id == "btn-delete":
            self._delete_profile()
        elif event.button.id == "btn-toggle-mode":
            self._toggle_mode()
        elif event.button.id == "btn-save":
            self._save_permissions()

    def _dept_options(self) -> list[tuple[str, Any]]:
        return [(d["name"], d["id"]) for d in getattr(self, "_depts", [])]

    @staticmethod
    def _parse_json_obj(raw: str) -> dict | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            return None

    @work(exclusive=True)
    async def _new_profile(self) -> None:
        result = await self.app.push_screen_wait(
            _ProfileForm(dept_options=self._dept_options())
        )
        if not result:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.tool_service import create_interface_profile  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        if not org_id:
            self.notify("Select an org first (F3)", severity="warning")
            return
        try:
            dept_raw = result.get("dept_id") or ""
            dept_id = uuid.UUID(dept_raw) if dept_raw else None
            async with get_session() as db:
                profile = await create_interface_profile(
                    db,
                    org_id=uuid.UUID(org_id),
                    interface=result.get("interface", "api"),
                    mode=result.get("mode", "allowlist"),
                    description=result.get("description", ""),
                    dept_id=dept_id,
                    system_prompt=(result.get("system_prompt") or None),
                    interface_config=self._parse_json_obj(result.get("interface_config", "")),
                    preferences=self._parse_json_obj(result.get("preferences", "")),
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
        perm_tags = self.query_one("#perm-shuttle", ShuttleWidget).get_assigned()
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.tool_service import update_interface_profile  # noqa: PLC0415

        try:
            async with get_session() as db:
                await update_interface_profile(db, uuid.UUID(pid), tool_permissions=perm_tags)
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("profile_perms_update", pid, {"count": len(perm_tags)})
            self.notify("Saved")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _edit_profile(self) -> None:
        pid = getattr(self, "_selected_profile_id", None)
        if not pid:
            self.notify("Select a profile first", severity="warning")
            return
        profile = getattr(self, "_profiles", {}).get(pid, {})
        result = await self.app.push_screen_wait(
            _ProfileForm(initial=profile, dept_options=self._dept_options())
        )
        if not result:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.tool_service import update_interface_profile  # noqa: PLC0415

        try:
            dept_raw = result.get("dept_id") or ""
            dept_id = uuid.UUID(dept_raw) if dept_raw else None
            fields: dict[str, Any] = {
                "interface": result.get("interface", profile.get("interface", "api")),
                "mode": result.get("mode", profile.get("mode", "allowlist")),
                "description": (result.get("description") or "") or None,
                "system_prompt": (result.get("system_prompt") or "") or None,
                "interface_config": self._parse_json_obj(result.get("interface_config", "")),
                "preferences": self._parse_json_obj(result.get("preferences", "")),
                "dept_id": dept_id,
            }
            async with get_session() as db:
                await update_interface_profile(db, uuid.UUID(pid), **fields)
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("profile_update", pid, {"interface": fields["interface"], "dept_id": str(dept_id) if dept_id else None})
            self.notify("Updated")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _delete_profile(self) -> None:
        pid = getattr(self, "_selected_profile_id", None)
        if not pid:
            self.notify("Select a profile first", severity="warning")
            return
        confirmed = await self.app.push_screen_wait(QuickConfirmDialog("Delete this interface profile?"))
        if not confirmed:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.tool_service import delete_interface_profile  # noqa: PLC0415

        try:
            async with get_session() as db:
                await delete_interface_profile(db, uuid.UUID(pid))
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("profile_delete", pid, {})
            self.notify("Deleted")
            self._selected_profile_id = None
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")
