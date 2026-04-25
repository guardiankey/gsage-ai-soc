"""ApiKeysPanel — API key DataTable + create/revoke."""

from __future__ import annotations

import uuid

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable

from admin_console.widgets.copy_dialog import CopyDialog
from admin_console.widgets.form_screen import FormField, FormScreen
from admin_console.widgets.quick_confirm_dialog import QuickConfirmDialog
from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.kv_panel import KVPanel


class _ApiKeyForm(FormScreen):
    TITLE = "New API Key"
    FIELDS = [
        FormField("name", "Name", required=True, placeholder="My integration key"),
        FormField(
            "environment",
            "Environment",
            field_type="select",
            options=[("live", "live"), ("test", "test")],
            default="live",
        ),
        FormField(
            "interface",
            "Interface (optional)",
            field_type="select",
            options=[
                ("api", "api"),
                ("web", "web"),
                ("cli", "cli"),
                ("email", "email"),
                ("telegram", "telegram"),
                ("whatsapp", "whatsapp"),
                ("slack", "slack"),
            ],
        ),
        FormField(
            "rate_limit_per_minute",
            "Rate limit (req/min)",
            default="60",
            placeholder="60",
        ),
        FormField(
            "scoped_permissions",
            "Scoped permissions",
            field_type="textarea",
            placeholder="Leave empty to inherit all org permissions.\nOne tag per line or comma-separated.",
        ),
    ]


class ApiKeysPanel(Widget):
    DEFAULT_CSS = """
    ApiKeysPanel {
        height: 1fr;
        padding: 1;
        layout: horizontal;
    }
    ApiKeysPanel #left-col { width: 1fr; height: 1fr; }
    ApiKeysPanel #btn-row { height: 3; layout: horizontal; }
    ApiKeysPanel #btn-row Button { margin-right: 1; }
    ApiKeysPanel KVPanel { width: 44; height: 1fr; margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="left-col"):
            with Horizontal(id="btn-row"):
                yield Button("New", id="btn-new", variant="primary")
                yield Button("Revoke", id="btn-revoke", variant="error")
                yield Button("Refresh", id="btn-refresh")
            yield DataTableExt(
                columns=["Name", "Prefix", "Org", "User", "Active", "Expires", "Created"],
                id="key-table",
            )
        yield KVPanel(title="Detail", id="key-detail")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from sqlalchemy import select  # noqa: PLC0415

        from src.shared.models.api_key import GSageAPIKey  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        try:
            async with get_session() as db:
                q = select(GSageAPIKey).order_by(GSageAPIKey.created_at.desc()).limit(200)
                if org_id:
                    q = q.where(GSageAPIKey.org_id == uuid.UUID(org_id))
                result = await db.execute(q)
                keys = result.scalars().all()

            self._keys = {str(k.id): k for k in keys}
            table = self.query_one("#key-table", DataTableExt)
            table.set_rows(
                [[
                    k.name or "—",
                    k.key_prefix or "—",
                    str(k.org_id)[:8],
                    str(k.user_id)[:8] if k.user_id else "—",
                    "✓" if k.is_active else "✗",
                    str(k.expires_at)[:10] if k.expires_at else "never",
                    str(k.created_at)[:10],
                ] for k in keys],
                [str(k.id) for k in keys],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        kid = str(event.row_key.value)
        k = getattr(self, "_keys", {}).get(kid)
        if k:
            self.query_one("#key-detail", KVPanel).update({
                "id": str(k.id),
                "name": k.name,
                "prefix": k.key_prefix,
                "org_id": str(k.org_id),
                "user_id": str(k.user_id) if k.user_id else "",
                "is_active": k.is_active,
                "expires_at": str(k.expires_at) if k.expires_at else "never",
                "created_at": str(k.created_at),
            })

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()
        elif event.button.id == "btn-new":
            self._new_key()
        elif event.button.id == "btn-revoke":
            self._revoke_key()

    @work(exclusive=True)
    async def _new_key(self) -> None:
        org_id = getattr(self.app, "active_org_id", None)
        if not org_id:
            self.notify("Select an organization first (F3)", severity="warning")
            return

        result = await self.app.push_screen_wait(_ApiKeyForm())
        if not result:
            return

        name: str = (result.get("name") or "").strip()
        if not name:
            self.notify("Name is required", severity="error")
            return

        environment: str = result.get("environment") or "live"
        interface_val = result.get("interface")
        interface = interface_val if interface_val and interface_val not in ("", None) else None

        try:
            rate_limit = int(result.get("rate_limit_per_minute") or 60)
        except (ValueError, TypeError):
            rate_limit = 60

        raw_perms: str = result.get("scoped_permissions") or ""
        scoped: list[str] = [
            t.strip()
            for t in raw_perms.replace(",", "\n").splitlines()
            if t.strip()
        ]

        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.api_key_service import create_api_key  # noqa: PLC0415

        try:
            async with get_session() as db:
                raw_key, key_dict = await create_api_key(
                    db,
                    org_id=uuid.UUID(org_id),
                    name=name,
                    environment=environment,
                    scoped_permissions=scoped,
                    interface=interface,
                    rate_limit_per_minute=rate_limit,
                )
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("apikey_create", key_dict["id"], {"name": name}, org_id=org_id)
            self.load_data()
            await self.app.push_screen_wait(
                CopyDialog("New API Key — save this now, it won't be shown again!", raw_key)
            )
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _revoke_key(self) -> None:
        table = self.query_one("#key-table", DataTableExt)
        kid = table.selected_key
        if not kid:
            self.notify("Select a key first", severity="warning")
            return

        confirmed = await self.app.push_screen_wait(QuickConfirmDialog("Revoke this API key?"))
        if not confirmed:
            return

        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from sqlalchemy import update  # noqa: PLC0415

        from src.shared.models.api_key import GSageAPIKey  # noqa: PLC0415

        try:
            async with get_session() as db:
                await db.execute(
                    update(GSageAPIKey)
                    .where(GSageAPIKey.id == uuid.UUID(kid))
                    .values(is_active=False)
                )
                await db.commit()
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("apikey_revoke", kid, {})
            self.notify("Key revoked")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")
