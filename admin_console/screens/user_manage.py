"""UserManagePanel — User DataTable + create/edit + OTP/password reset."""

from __future__ import annotations

import uuid

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable

from admin_console.widgets.copy_dialog import CopyDialog
from admin_console.widgets.quick_confirm_dialog import QuickConfirmDialog
from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.form_screen import FormField, FormScreen
from admin_console.widgets.kv_panel import KVPanel


class _UserForm(FormScreen):
    TITLE = "User"
    FIELDS = [
        FormField("email", "Email", required=True),
        FormField("full_name", "Full Name"),
        FormField("password", "Password (leave blank to keep)", field_type="password"),
        FormField("is_active", "Active", field_type="switch", default=True),
        FormField("is_superuser", "Superuser", field_type="switch", default=False),
    ]


class UserManagePanel(Widget):
    DEFAULT_CSS = """
    UserManagePanel {
        height: 1fr;
        padding: 1;
        layout: horizontal;
    }
    UserManagePanel #left-col {
        width: 1fr;
        height: 1fr;
    }
    UserManagePanel #btn-row {
        height: 3;
        layout: horizontal;
    }
    UserManagePanel #btn-row Button {
        margin-right: 1;
    }
    UserManagePanel KVPanel {
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
                yield Button("Toggle", id="btn-toggle")
                yield Button("Reset PW", id="btn-pw")
                yield Button("Reset OTP", id="btn-otp")
                yield Button("Refresh", id="btn-refresh")
            yield DataTableExt(
                columns=["Org", "Email", "Full Name", "Active", "Superuser", "OTP", "Created"],
                id="user-table",
            )
        yield KVPanel(title="Detail", id="user-detail")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.user_service import list_users  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        dept_id = getattr(self.app, "active_dept_id", None)
        org_name = getattr(self.app, "active_org_name", "") or ""
        try:
            async with get_session() as db:
                users = await list_users(
                    db,
                    org_id=uuid.UUID(org_id) if org_id else None,
                    dept_id=uuid.UUID(dept_id) if dept_id else None,
                    org_name=org_name if org_id else None,
                )
            self._users = {u["id"]: u for u in users}
            table = self.query_one("#user-table", DataTableExt)
            table.set_rows(
                [[u.get("org_name", ""), u["email"], u.get("full_name", ""),
                  "✓" if u["is_active"] else "✗",
                  "✓" if u.get("is_superuser") else "",
                  "✓" if u.get("otp_enabled") else "",
                  u["created_at"][:10]] for u in users],
                [u["id"] for u in users],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "btn-refresh": self.load_data,
            "btn-new": self._new_user,
            "btn-edit": self._edit_user,
            "btn-toggle": self._toggle_user,
            "btn-pw": self._reset_password,
            "btn-otp": self._reset_otp,
        }
        fn = actions.get(event.button.id or "")
        if fn:
            fn()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        uid = str(event.row_key.value)
        user = getattr(self, "_users", {}).get(uid, {})
        if user:
            safe = {k: v for k, v in user.items() if "password" not in k and "otp" not in k}
            self.query_one("#user-detail", KVPanel).update(safe, title=user.get("email", ""))

    def _selected_id(self) -> str | None:
        try:
            return self.query_one("#user-table", DataTableExt).selected_key
        except Exception:
            return None

    @work(exclusive=True)
    async def _new_user(self) -> None:
        org_id = getattr(self.app, "active_org_id", None)
        if not org_id:
            self.notify("Select an organization first (F3)", severity="warning")
            return
        result = await self.app.push_screen_wait(_UserForm())
        if not result:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.user_service import (  # noqa: PLC0415
            create_user,
            get_user_by_email,
            link_user_to_org,
        )

        try:
            async with get_session() as db:
                user = await create_user(
                    db,
                    email=result["email"],
                    full_name=result.get("full_name", ""),
                    password=result.get("password", ""),
                    org_id=uuid.UUID(org_id),
                )
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("user_create", user["id"], {"email": user["email"]}, org_id=org_id)
            self.notify(f"Created: {user['email']}")
            self.load_data()
        except Exception as exc:
            # Detect duplicate email (IntegrityError / unique constraint violation)
            err_lower = str(exc).lower()
            if "unique" in err_lower or "duplicate" in err_lower:
                async with get_session() as db:
                    existing = await get_user_by_email(db, result["email"])
                if existing:
                    confirmed = await self.app.push_screen_wait(
                        QuickConfirmDialog(
                            f"'{result['email']}' already exists. Link to this org?"
                        )
                    )
                    if confirmed:
                        try:
                            async with get_session() as db:
                                await link_user_to_org(db, uuid.UUID(existing["id"]), uuid.UUID(org_id))
                            from admin_console.audit import log_event  # noqa: PLC0415
                            log_event("user_link_org", existing["id"], {"email": existing["email"]}, org_id=org_id)
                            self.notify(f"Linked: {result['email']}")
                            self.load_data()
                        except Exception as link_exc:
                            self.notify(str(link_exc), severity="error")
                    return
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _edit_user(self) -> None:
        uid = self._selected_id()
        if not uid:
            self.notify("Select a user first", severity="warning")
            return
        user = getattr(self, "_users", {}).get(uid, {})
        result = await self.app.push_screen_wait(_UserForm(initial=user))
        if not result:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.user_service import update_user  # noqa: PLC0415

        # Only update allowed fields; skip empty password
        updatable = {
            k: v for k, v in result.items()
            if k in ("email", "full_name", "is_active") or (k == "password" and v)
        }
        try:
            async with get_session() as db:
                await update_user(db, uuid.UUID(uid), **updatable)
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("user_update", uid, {"email": result.get("email")})
            self.notify("Updated")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _toggle_user(self) -> None:
        uid = self._selected_id()
        if not uid:
            self.notify("Select a user first", severity="warning")
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.user_service import toggle_user_active  # noqa: PLC0415

        try:
            async with get_session() as db:
                result = await toggle_user_active(db, uuid.UUID(uid))
            new_state = result["is_active"] if result else "unknown"
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("user_toggle", uid, {"is_active": new_state})
            self.notify(f"is_active → {new_state}")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _reset_password(self) -> None:
        uid = self._selected_id()
        if not uid:
            self.notify("Select a user first", severity="warning")
            return
        confirmed = await self.app.push_screen_wait(QuickConfirmDialog("Reset this user's password to a random value?"))
        if not confirmed:
            return
        import secrets  # noqa: PLC0415
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.user_service import reset_password  # noqa: PLC0415

        try:
            new_pw = secrets.token_urlsafe(16)
            async with get_session() as db:
                await reset_password(db, uuid.UUID(uid), new_pw)
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("user_reset_password", uid, {})
            await self.app.push_screen_wait(CopyDialog("New Password", new_pw))
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _reset_otp(self) -> None:
        uid = self._selected_id()
        if not uid:
            self.notify("Select a user first", severity="warning")
            return
        confirmed = await self.app.push_screen_wait(QuickConfirmDialog("Reset / disable OTP for this user?"))
        if not confirmed:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.user_service import reset_otp  # noqa: PLC0415

        try:
            async with get_session() as db:
                await reset_otp(db, uuid.UUID(uid))
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("user_reset_otp", uid, {})
            self.notify("OTP disabled")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")
