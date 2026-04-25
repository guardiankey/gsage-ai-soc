"""OrgManagePanel — Organization DataTable with create/edit/toggle."""

from __future__ import annotations

import uuid
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable

from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.quick_confirm_dialog import QuickConfirmDialog
from admin_console.widgets.form_screen import FormField, FormScreen
from admin_console.widgets.kv_panel import KVPanel


class _OrgForm(FormScreen):
    TITLE = "Organization"
    FIELDS = [
        FormField("name", "Name", required=True),
        FormField("slug", "Slug", required=True),
        FormField("llm_provider", "LLM Provider", placeholder="ollama"),
        FormField("llm_api_key", "LLM API Key", field_type="password"),
        FormField("default_maker_model", "Maker Model", placeholder="llama3.1:8b"),
        FormField("default_reviewer_model", "Reviewer Model", placeholder="llama3.1:8b"),
        FormField("agent_timeout_seconds", "Agent Timeout (s)", placeholder="120"),
        FormField("max_context_tokens", "Max Context Tokens", placeholder="6000"),
        FormField("auth_providers", "Auth Providers", placeholder="local"),
        FormField("system_prompt", "System Prompt", field_type="textarea"),
        FormField("is_active", "Active", field_type="switch", default=True),
    ]


class _SmtpForm(FormScreen):
    TITLE = "SMTP Configuration"
    FIELDS = [
        FormField("host", "SMTP Host", required=True, placeholder="smtp.example.com"),
        FormField("port", "Port", default="587", placeholder="587"),
        FormField("username", "Username", placeholder="user@example.com"),
        FormField("password", "Password", field_type="password"),
        FormField("use_tls", "Use TLS", field_type="switch", default=True),
        FormField("from_email", "From Email", placeholder="noreply@example.com"),
        FormField("from_name", "From Name", placeholder="gSage AI"),
        FormField(
            "default_format",
            "Email Format",
            field_type="select",
            options=[("HTML", "html"), ("Plain Text", "text")],
            default="html",
        ),
    ]


class _AuthConfigForm(FormScreen):
    TITLE = "Auth Configuration (JSON)"
    FIELDS = [
        FormField(
            "auth_config_json",
            "Auth Config JSON",
            field_type="textarea",
        ),
    ]


class OrgManagePanel(Widget):
    DEFAULT_CSS = """
    OrgManagePanel {
        height: 1fr;
        padding: 1;
        layout: horizontal;
    }
    OrgManagePanel #left-col {
        width: 1fr;
        height: 1fr;
    }
    OrgManagePanel #btn-row {
        height: 3;
        layout: horizontal;
    }
    OrgManagePanel #btn-row Button {
        margin-right: 1;
    }
    OrgManagePanel KVPanel {
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
                yield Button("SMTP", id="btn-smtp")
                yield Button("Auth Config", id="btn-auth")
                yield Button("Toggle Active", id="btn-toggle")
                yield Button("Refresh", id="btn-refresh")
            yield DataTableExt(
                columns=["Name", "Slug", "Active", "LLM", "Created"],
                id="org-table",
            )
        yield KVPanel(title="Detail", id="org-detail")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.org_service import list_orgs  # noqa: PLC0415

        try:
            async with get_session() as db:
                orgs = await list_orgs(db)
            self._orgs = {o["id"]: o for o in orgs}
            table = self.query_one("#org-table", DataTableExt)
            table.set_rows(
                [[o["name"], o["slug"], "✓" if o["is_active"] else "✗",
                  o.get("llm_provider", "—"), o["created_at"][:10]] for o in orgs],
                [o["id"] for o in orgs],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()
        elif event.button.id == "btn-new":
            self._new_org()
        elif event.button.id == "btn-edit":
            self._edit_org()
        elif event.button.id == "btn-smtp":
            self._edit_smtp()
        elif event.button.id == "btn-auth":
            self._edit_auth_config()
        elif event.button.id == "btn-toggle":
            self._toggle_org()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        org_id = str(event.row_key.value)
        org = getattr(self, "_orgs", {}).get(org_id)
        if org:
            self.query_one("#org-detail", KVPanel).update(org, title=org["name"])

    def _selected_id(self) -> str | None:
        try:
            table = self.query_one("#org-table", DataTableExt)
            return table.selected_key
        except Exception:
            return None

    @work(exclusive=True)
    async def _new_org(self) -> None:
        result = await self.app.push_screen_wait(_OrgForm())
        if not result:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.org_service import create_org  # noqa: PLC0415

        try:
            async with get_session() as db:
                org = await create_org(
                    db,
                    name=result["name"],
                    slug=result.get("slug", ""),
                    llm_provider=result.get("llm_provider") or "ollama",
                    extra_fields=result,
                )
            self.notify(f"Created: {org['name']}")
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("org_create", org["id"], {"name": org["name"]})
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _edit_org(self) -> None:
        org_id = self._selected_id()
        if not org_id:
            self.notify("Select an org first", severity="warning")
            return
        org = getattr(self, "_orgs", {}).get(org_id, {})
        result = await self.app.push_screen_wait(_OrgForm(initial=org))
        if not result:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.org_service import update_org  # noqa: PLC0415

        try:
            async with get_session() as db:
                await update_org(db, uuid.UUID(org_id), **result)
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("org_update", org_id, {k: v for k, v in result.items() if k != "llm_api_key"})
            self.notify("Updated")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _edit_smtp(self) -> None:
        import json  # noqa: PLC0415

        org_id = self._selected_id()
        if not org_id:
            self.notify("Select an org first", severity="warning")
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.org_service import get_org_model  # noqa: PLC0415

        try:
            async with get_session() as db:
                org_model = await get_org_model(db, uuid.UUID(org_id))
            current: dict = (org_model.smtp_config or {}) if org_model else {}
        except Exception as exc:
            self.notify(f"Load smtp_config failed: {exc}", severity="error")
            return

        result = await self.app.push_screen_wait(_SmtpForm(initial=current))
        if not result:
            return

        smtp_dict = {
            "host": result.get("host", ""),
            "port": int(result.get("port") or 587),
            "username": result.get("username", ""),
            "password": result.get("password", ""),
            "use_tls": bool(result.get("use_tls", True)),
            "from_email": result.get("from_email", ""),
            "from_name": result.get("from_name", ""),
            "default_format": result.get("default_format") or "html",
        }
        try:
            from admin_console.services.org_service import update_org_smtp  # noqa: PLC0415

            async with get_session() as db:
                await update_org_smtp(db, uuid.UUID(org_id), smtp_dict)
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("org_smtp_update", org_id, {"host": smtp_dict["host"]})
            self.notify("SMTP config saved")
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _edit_auth_config(self) -> None:
        import json  # noqa: PLC0415

        org_id = self._selected_id()
        if not org_id:
            self.notify("Select an org first", severity="warning")
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.org_service import get_org_model  # noqa: PLC0415

        try:
            async with get_session() as db:
                org_model = await get_org_model(db, uuid.UUID(org_id))
            current_cfg: dict = (org_model.auth_config or {}) if org_model else {}
        except Exception as exc:
            self.notify(f"Load auth_config failed: {exc}", severity="error")
            return

        initial_json = json.dumps(current_cfg, indent=2) if current_cfg else "{}"
        result = await self.app.push_screen_wait(
            _AuthConfigForm(initial={"auth_config_json": initial_json})
        )
        if not result:
            return

        raw = (result.get("auth_config_json") or "").strip()
        try:
            cfg = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            self.notify(f"Invalid JSON: {exc}", severity="error")
            return

        try:
            from admin_console.services.org_service import update_org_auth_config  # noqa: PLC0415

            async with get_session() as db:
                await update_org_auth_config(db, uuid.UUID(org_id), cfg)
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("org_auth_config_update", org_id, {})
            self.notify("Auth config saved")
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _toggle_org(self) -> None:
        org_id = self._selected_id()
        if not org_id:
            self.notify("Select an org first", severity="warning")
            return
        confirmed = await self.app.push_screen_wait(QuickConfirmDialog("Toggle org active status?"))
        if not confirmed:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.org_service import toggle_org_active  # noqa: PLC0415

        try:
            async with get_session() as db:
                new_state = await toggle_org_active(db, uuid.UUID(org_id))
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("org_toggle", org_id, {"is_active": new_state})
            self.notify(f"is_active → {new_state}")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")
