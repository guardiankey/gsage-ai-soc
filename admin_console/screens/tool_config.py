"""ToolConfigPanel — two-pane: tools list + JSON editor for config."""

from __future__ import annotations

import json
import uuid

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, TextArea

from admin_console.widgets.quick_confirm_dialog import QuickConfirmDialog
from admin_console.widgets.data_table_ext import DataTableExt
from admin_console.widgets.form_screen import FormField, FormScreen
from admin_console.widgets.kv_panel import KVPanel


class _ToolForm(FormScreen):
    TITLE = "Tool Config"
    FIELDS = [
        FormField("tool_name", "Tool Name", required=True),
        FormField("is_enabled", "Enabled", field_type="switch", default=True),
    ]


class ToolConfigPanel(Widget):
    DEFAULT_CSS = """
    ToolConfigPanel {
        height: 1fr;
        padding: 1;
    }
    ToolConfigPanel #btn-row { height: 3; layout: horizontal; }
    ToolConfigPanel #btn-row Button { margin-right: 1; }
    ToolConfigPanel #main-row { height: 1fr; layout: horizontal; }
    ToolConfigPanel #left-col { width: 1fr; height: 1fr; }
    ToolConfigPanel #right-col {
        width: 50;
        height: 1fr;
        margin-left: 1;
    }
    ToolConfigPanel TextArea {
        height: 1fr;
    }
    ToolConfigPanel #save-btn { margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="btn-row"):
            yield Button("New", id="btn-new", variant="primary")
            yield Button("Delete", id="btn-delete", variant="error")
            yield Button("Refresh", id="btn-refresh")
        with Horizontal(id="main-row"):
            with Vertical(id="left-col"):
                yield DataTableExt(
                    columns=["Tool Name", "Org", "Enabled"],
                    id="tool-table",
                )
            with Vertical(id="right-col"):
                yield KVPanel(title="Tool Info", id="tool-info")
                yield TextArea(language="json", id="config-editor")
                yield Button("Save Config", id="save-btn", variant="primary")

    def on_mount(self) -> None:
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.tool_service import list_tool_configs  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        try:
            async with get_session() as db:
                tools = await list_tool_configs(db, uuid.UUID(org_id)) if org_id else []
            self._tools = {t["id"]: t for t in tools}
            table = self.query_one("#tool-table", DataTableExt)
            table.set_rows(
                [[t["tool_name"], str(t.get("org_id", ""))[:8], "✓" if t.get("is_enabled") else "✗"] for t in tools],
                [t["id"] for t in tools],
            )
            # Clear detail panel when list is refreshed
            self.query_one("#tool-info", KVPanel).update({})
            try:
                self.query_one("#config-editor", TextArea).load_text("{}")
            except Exception:
                pass
            self._selected_tool_id = None
        except Exception as exc:
            self.notify(f"Load error: {exc}", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        tid = str(event.row_key.value)
        tool = getattr(self, "_tools", {}).get(tid, {})
        if tool:
            info = {k: v for k, v in tool.items() if k != "config"}
            self.query_one("#tool-info", KVPanel).update(info)
            config = tool.get("config", {})
            try:
                self.query_one("#config-editor", TextArea).load_text(
                    json.dumps(config, indent=2) if config else "{}"
                )
            except Exception:
                pass
            self._selected_tool_id = tid

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "btn-refresh": self.load_data,
            "btn-new": self._new_tool,
            "btn-delete": self._delete_tool,
            "save-btn": self._save_config,
        }
        fn = actions.get(event.button.id or "")
        if fn:
            fn()

    @work(exclusive=True)
    async def _new_tool(self) -> None:
        result = await self.app.push_screen_wait(_ToolForm())
        if not result:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.tool_service import create_tool_config  # noqa: PLC0415

        org_id = getattr(self.app, "active_org_id", None)
        try:
            async with get_session() as db:
                tool = await create_tool_config(
                    db,
                    org_id=uuid.UUID(org_id) if org_id else uuid.uuid4(),
                    tool_name=result.get("tool_name", ""),
                    profile_id=result.get("profile_id", ""),
                    config=result.get("config", {}),
                    description=result.get("description", ""),
                )
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("tool_create", tool["id"], {"tool_name": tool["tool_name"]}, org_id=org_id)
            self.notify(f"Created: {tool['tool_name']}")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _save_config(self) -> None:
        tid = getattr(self, "_selected_tool_id", None)
        if not tid:
            self.notify("Select a tool first", severity="warning")
            return
        raw = self.query_one("#config-editor", TextArea).text
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.notify(f"Invalid JSON: {exc}", severity="error")
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.tool_service import update_tool_config  # noqa: PLC0415

        try:
            async with get_session() as db:
                await update_tool_config(db, uuid.UUID(tid), {"config": config})
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("tool_update_config", tid, {})
            self.notify("Config saved")
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def _delete_tool(self) -> None:
        tid = getattr(self, "_selected_tool_id", None)
        if not tid:
            self.notify("Select a tool first", severity="warning")
            return
        confirmed = await self.app.push_screen_wait(QuickConfirmDialog("Delete this tool config?"))
        if not confirmed:
            return
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.tool_service import delete_tool_config  # noqa: PLC0415

        try:
            async with get_session() as db:
                await delete_tool_config(db, uuid.UUID(tid))
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("tool_delete", tid, {})
            self.notify("Deleted")
            self._selected_tool_id = None
            self.load_data()
        except Exception as exc:
            self.notify(str(exc), severity="error")
