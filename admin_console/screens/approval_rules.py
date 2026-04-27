"""ApprovalRulesPanel — Rules DataTable + CRUD."""

from __future__ import annotations

import uuid

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable

from admin_console.widgets.quick_confirm_dialog import QuickConfirmDialog
from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.form_screen import FormField, FormScreen
from admin_console.widgets.kv_panel import KVPanel


class _RuleForm(FormScreen):
    TITLE = "Approval Rule"
    FIELDS = [
        FormField("org_id_pattern", "Org ID Pattern", default="*"),
        FormField("dept_id_pattern", "Dept ID Pattern", default="*"),
        FormField("user_id_pattern", "User ID Pattern", default="*"),
        FormField("tool_pattern", "Tool Pattern", required=True, default="*"),
        FormField("approver_email", "Approver Email", required=True),
        FormField("priority", "Priority", default="0"),
        FormField("description", "Description"),
        FormField("is_active", "Active", field_type="switch", default=True),
    ]


class ApprovalRulesPanel(Widget):
    DEFAULT_CSS = """
    ApprovalRulesPanel {
        height: 1fr;
        padding: 1;
        layout: horizontal;
    }
    ApprovalRulesPanel #left-col { width: 1fr; height: 1fr; }
    ApprovalRulesPanel #btn-row { height: 3; layout: horizontal; }
    ApprovalRulesPanel #btn-row Button { margin-right: 1; }
    ApprovalRulesPanel KVPanel { width: 44; height: 1fr; margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="left-col"):
            with Horizontal(id="btn-row"):
                yield Button("New", id="btn-new", variant="primary")
                yield Button("Edit", id="btn-edit")
                yield Button("Delete", id="btn-delete", variant="error")
                yield Button("Refresh", id="btn-refresh")
            yield DataTableExt(
                columns=["Tool Pattern", "Dept Pattern", "Org Pattern", "User Pattern", "Priority", "Active"],
                id="rule-table",
            )
        yield KVPanel(title="Rule Detail", id="rule-detail")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.approval_service import list_approval_rules  # noqa: PLC0415

        try:
            async with get_session() as db:
                rules = await list_approval_rules(db)
            self._rules = {r["id"]: r for r in rules}
            table = self.query_one("#rule-table", DataTableExt)
            table.set_rows(
                [[r["tool_pattern"], r["dept_id_pattern"], r["org_id_pattern"], r["user_id_pattern"],
                  str(r["priority"]), "✓" if r["is_active"] else "✗"] for r in rules],
                [r["id"] for r in rules],
            )
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        rid = str(event.row_key.value)
        rule = getattr(self, "_rules", {}).get(rid, {})
        if rule:
            self.query_one("#rule-detail", KVPanel).update(rule)

    def _selected_id(self) -> str | None:
        try:
            dt = self.query_one("#rule-table DataTable", DataTable)
            rk = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
            return str(rk.value)
        except Exception:
            return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "btn-refresh": self.load_data,
            "btn-new": self._new_rule,
            "btn-edit": self._edit_rule,
            "btn-delete": self._delete_rule,
        }
        fn = actions.get(event.button.id or "")
        if fn:
            fn()

    @work(exclusive=True)
    async def _new_rule(self) -> None:
        result = await self.app.push_screen_wait(_RuleForm())
        if not result:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.approval_service import create_approval_rule  # noqa: PLC0415
        from admin_console.services.user_service import get_user_by_email  # noqa: PLC0415

        try:
            email = (result.get("approver_email") or "").strip()
            if not email:
                self.notify("Approver Email is required", severity="error")
                return
            async with get_session() as db:
                approver = await get_user_by_email(db, email)
                if not approver:
                    self.notify(f"User not found: {email}", severity="error")
                    return
                result["priority"] = int(result["priority"]) if result.get("priority") not in (None, "") else 0
                result["is_active"] = result.get("is_active") in (True, "true", "True", "1", 1)
                rule = await create_approval_rule(
                    db,
                    org_id_pattern=result.get("org_id_pattern", "*"),
                    user_id_pattern=result.get("user_id_pattern", "*"),
                    dept_id_pattern=result.get("dept_id_pattern", "*"),
                    tool_pattern=result.get("tool_pattern", "*"),
                    approver_user_id=uuid.UUID(approver["id"]),
                    priority=result.get("priority", 100),
                    description=result.get("description", ""),
                )
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("approval_rule_create", rule["id"], {"tool_pattern": rule["tool_pattern"]})
            self.notify("Created")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _edit_rule(self) -> None:
        rid = self._selected_id()
        if not rid:
            self.notify("Select a rule", severity="warning")
            return
        rule = dict(getattr(self, "_rules", {}).get(rid, {}))
        # Reverse-lookup approver email for prefill
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.user_service import get_user, get_user_by_email  # noqa: PLC0415
        try:
            approver_uuid = rule.get("approver_user_id")
            if approver_uuid:
                async with get_session() as db:
                    approver = await get_user(db, uuid.UUID(str(approver_uuid)))
                if approver:
                    rule["approver_email"] = approver.get("email", "")
        except Exception:
            pass
        result = await self.app.push_screen_wait(_RuleForm(initial=rule))
        if not result:
            return
        from admin_console.services.approval_service import update_approval_rule  # noqa: PLC0415

        try:
            email = (result.get("approver_email") or "").strip()
            if not email:
                self.notify("Approver Email is required", severity="error")
                return
            async with get_session() as db:
                approver = await get_user_by_email(db, email)
                if not approver:
                    self.notify(f"User not found: {email}", severity="error")
                    return
                result["priority"] = int(result["priority"]) if result.get("priority") not in (None, "") else 0
                result["is_active"] = result.get("is_active") in (True, "true", "True", "1", 1)
                result.pop("approver_email", None)
                result["approver_user_id"] = uuid.UUID(approver["id"])
                await update_approval_rule(db, uuid.UUID(rid), **result)
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("approval_rule_update", rid, {"approver_email": email})
            self.notify("Updated")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _delete_rule(self) -> None:
        rid = self._selected_id()
        if not rid:
            self.notify("Select a rule", severity="warning")
            return
        confirmed = await self.app.push_screen_wait(QuickConfirmDialog("Delete this approval rule?"))
        if not confirmed:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.approval_service import delete_approval_rule  # noqa: PLC0415

        try:
            async with get_session() as db:
                await delete_approval_rule(db, uuid.UUID(rid))
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("approval_rule_delete", rid, {})
            self.notify("Deleted")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")
