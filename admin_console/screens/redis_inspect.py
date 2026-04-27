"""RedisInspectPanel — DB selector + key browser + flush + queues + RedBeat."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Select, TabbedContent, TabPane, TextArea

from admin_console.widgets.confirm_dialog import ConfirmDialog
from admin_console.widgets.kv_panel import KVPanel


class RedisInspectPanel(Widget):
    DEFAULT_CSS = """
    RedisInspectPanel {
        height: 1fr;
        padding: 1;
    }
    RedisInspectPanel #ctrl-row { height: 3; layout: horizontal; }
    RedisInspectPanel #ctrl-row Button { margin-right: 1; }
    RedisInspectPanel Select { width: 22; }
    RedisInspectPanel #pattern-input { width: 20; }
    RedisInspectPanel #key-val { height: 1fr; }
    RedisInspectPanel #keys-row { height: 1fr; layout: horizontal; }
    RedisInspectPanel #keys-table { width: 1fr; height: 1fr; }
    RedisInspectPanel #key-value { width: 1fr; height: 1fr; margin-left: 1; border: round #555753; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="ctrl-row"):
            yield Select(
                options=[("DB 0", 0), ("DB 1 (Celery)", 1), ("DB 2", 2)],
                value=0,
                id="db-select",
            )
            yield Input(value="*", placeholder="pattern", id="pattern-input")
            yield Button("Scan Keys", id="btn-scan", variant="primary")
            yield Button("Flush DB", id="btn-flush", variant="error")
            yield Button("Refresh Info", id="btn-info")
        with TabbedContent():
            with TabPane("Keys"):
                with Horizontal(id="keys-row"):
                    yield DataTable(id="keys-table", cursor_type="row")
                    yield TextArea("", id="key-value", read_only=True)
            with TabPane("Server Info"):
                yield KVPanel(title="Redis Info", id="redis-info")
            with TabPane("Queues"):
                yield KVPanel(title="Queue Lengths", id="queue-lengths")
            with TabPane("RedBeat"):
                yield KVPanel(title="Scheduled Tasks", id="redbeat-keys")

    def on_mount(self) -> None:
        table = self.query_one("#keys-table", DataTable)
        table.add_columns("Key", "Type", "TTL")
        self.load_info()

    @work(exclusive=True)
    async def load_info(self) -> None:
        import asyncio  # noqa: PLC0415

        from admin_console.db.redis_client import (  # noqa: PLC0415
            redis_info,
            redis_queue_lengths,
            redbeat_keys,
        )

        try:
            info = await asyncio.to_thread(redis_info)
            queues = await asyncio.to_thread(redis_queue_lengths)
            rb_keys = await asyncio.to_thread(redbeat_keys)

            self.query_one("#redis-info", KVPanel).update(info)
            self.query_one("#queue-lengths", KVPanel).update(queues)
            rb_data = {k: "" for k in rb_keys[:100]}
            self.query_one("#redbeat-keys", KVPanel).update(rb_data, title=f"RedBeat ({len(rb_keys)} keys)")
        except Exception as exc:
            self.notify(f"Redis error: {exc}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-scan":
            self._scan_keys()
        elif event.button.id == "btn-flush":
            self._flush_db()
        elif event.button.id == "btn-info":
            self.load_info()

    @work(exclusive=True)
    async def _scan_keys(self) -> None:
        import asyncio  # noqa: PLC0415

        from admin_console.db.redis_client import redis_key_ttl, redis_key_type, redis_scan_keys  # noqa: PLC0415

        db_val = self.query_one("#db-select", Select).value
        pattern = self.query_one("#pattern-input", Input).value or "*"
        db = int(str(db_val)) if db_val != Select.BLANK else 0
        try:
            keys = await asyncio.to_thread(redis_scan_keys, pattern, 200, db)
            table = self.query_one("#keys-table", DataTable)
            table.clear()
            for k in keys[:200]:
                typ = await asyncio.to_thread(redis_key_type, k, db)
                ttl = await asyncio.to_thread(redis_key_ttl, k, db)
                table.add_row(k, typ, ttl, key=k)
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            key = str(event.row_key.value) if event.row_key else ""
        except Exception:
            key = ""
        if key:
            self._show_key_value(key)

    @work(exclusive=True)
    async def _show_key_value(self, key: str) -> None:
        import asyncio  # noqa: PLC0415
        import json  # noqa: PLC0415

        from admin_console.db.redis_client import redis_get, redis_key_type  # noqa: PLC0415

        db_val = self.query_one("#db-select", Select).value
        db = int(str(db_val)) if db_val != Select.BLANK else 0
        ta = self.query_one("#key-value", TextArea)
        try:
            typ = (await asyncio.to_thread(redis_key_type, key, db)) or ""
            if typ.lower() != "string":
                ta.text = f"# Key type: {typ}\n# Preview only supports 'string' keys for now."
                return
            raw = await asyncio.to_thread(redis_get, key, db)
            if raw is None:
                ta.text = "(nil)"
                return
            try:
                parsed = json.loads(raw)
                ta.text = json.dumps(parsed, indent=2, ensure_ascii=False)
            except Exception:
                ta.text = raw
        except Exception as exc:
            ta.text = f"# Error: {exc}"

    @work(exclusive=True)
    async def _flush_db(self) -> None:
        db_val = self.query_one("#db-select", Select).value
        db = int(str(db_val)) if db_val != Select.BLANK else 0
        confirmed = await self.app.push_screen_wait(ConfirmDialog(f"Flush Redis DB {db}? This cannot be undone."))
        if not confirmed:
            return
        from admin_console.services.maintenance_service import flush_all_cache  # noqa: PLC0415

        ok, err = await flush_all_cache(db_index=db)
        if ok:
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("redis_flush", str(db), {})
            self.notify(f"DB {db} flushed")
        else:
            self.notify(f"Error: {err}", severity="error")
