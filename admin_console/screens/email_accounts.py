"""EmailAccountsPanel — Accounts DataTable + detail + password reveal."""

from __future__ import annotations

import uuid

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable

from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.kv_panel import KVPanel


class EmailAccountsPanel(Widget):
    DEFAULT_CSS = """
    EmailAccountsPanel {
        height: 1fr;
        padding: 1;
        layout: horizontal;
    }
    EmailAccountsPanel #left-col { width: 1fr; height: 1fr; }
    EmailAccountsPanel #btn-row { height: 3; layout: horizontal; }
    EmailAccountsPanel #btn-row Button { margin-right: 1; }
    EmailAccountsPanel #right-col { width: 50; height: 1fr; margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="left-col"):
            with Horizontal(id="btn-row"):
                yield Button("Reveal Passwords", id="btn-reveal", variant="warning")
                yield Button("Refresh", id="btn-refresh")
            yield DataTableExt(
                columns=["Email", "IMAP Host", "SMTP Host", "Active", "Org"],
                id="email-table",
            )
        with Vertical(id="right-col"):
            yield KVPanel(title="Account Detail", id="email-detail")

    def on_mount(self) -> None:
        self._reveal = False
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.email_service import list_email_accounts  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        try:
            async with get_session() as db:
                accounts = await list_email_accounts(
                    db, uuid.UUID(org_id)
                ) if org_id else []
            self._accounts = {a["id"]: a for a in accounts}
            table = self.query_one("#email-table", DataTableExt)
            table.set_rows(
                [[a["email"], a.get("imap_host", "—"), a.get("smtp_host", "—"),
                  "✓" if a.get("is_active") else "✗", str(a.get("org_id", ""))[:8]] for a in accounts],
                [a["id"] for a in accounts],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        aid = str(event.row_key.value)
        account = getattr(self, "_accounts", {}).get(aid, {})
        if account:
            if not self._reveal:
                display = {
                    k: ("****" if ("password" in k or "secret" in k) else v)
                    for k, v in account.items()
                }
            else:
                display = account
            self.query_one("#email-detail", KVPanel).update(display, title=account.get("email", ""))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()
        elif event.button.id == "btn-reveal":
            self._toggle_reveal()

    @work(exclusive=True)
    async def _toggle_reveal(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.email_service import get_email_account  # noqa: PLC0415

        if not self._reveal:
            # Load decrypted passwords
            org_id = getattr(self.app, "active_org_id", None)
            try:
                async with get_session() as db:
                    accounts = []
                    for aid in list(getattr(self, "_accounts", {}).keys()):
                        decrypted = await get_email_account(db, uuid.UUID(aid))
                        if decrypted:
                            accounts.append(decrypted)
                self._accounts = {a["id"]: a for a in accounts}
                self._reveal = True
                from admin_console.audit import log_event  # noqa: PLC0415
                log_event("email_reveal_passwords", "all", {}, org_id=org_id)
                self.notify("Passwords revealed — handle with care!", severity="warning")
            except Exception as exc:
                self.notify(str(exc), severity="error")
        else:
            self._reveal = False
            self.load_data()
            self.notify("Passwords hidden")
