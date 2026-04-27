"""MaintenancePanel — action cards for cache/DB/ES/Weaviate/diagnostics."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label, Static

from admin_console.widgets.confirm_dialog import ConfirmDialog
from admin_console.widgets.log_viewer import LogViewer


class _ActionCard(Static):
    DEFAULT_CSS = """
    _ActionCard {
        border: round #555753;
        padding: 1;
        height: auto;
        margin-bottom: 1;
    }
    _ActionCard Label#card-title {
        color: #729fcf;
        text-style: bold;
        height: 1;
    }
    _ActionCard Label#card-desc {
        color: #babdb6;
        height: 1;
        margin-bottom: 1;
    }
    """


class MaintenancePanel(Widget):
    DEFAULT_CSS = """
    MaintenancePanel {
        height: 1fr;
        padding: 1;
        layout: horizontal;
    }
    MaintenancePanel #left-col {
        width: 44;
        height: 1fr;
        overflow-y: auto;
    }
    MaintenancePanel #right-col {
        width: 1fr;
        height: 1fr;
        margin-left: 1;
    }
    MaintenancePanel Button {
        margin-right: 1;
        margin-top: 0;
    }
    """

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="left-col"):
            # Cache section
            with _ActionCard():
                yield Label("Cache Management", id="card-title")
                yield Label("Flush Redis caches selectively or fully.", id="card-desc")
                yield Button("Flush Permissions Cache", id="btn-flush-perms")
                yield Button("Flush API Keys Cache", id="btn-flush-apikeys")
                yield Button("Flush All (DB 0)", id="btn-flush-all", variant="error")

            # Weaviate section
            with _ActionCard():
                yield Label("Weaviate", id="card-title")
                yield Label("List collections and delete entire collections.", id="card-desc")
                yield Button("List Collections", id="btn-list-weaviate")

        with Vertical(id="right-col"):
            yield LogViewer(max_lines=200, title="Maintenance Log", id="maint-log")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "btn-flush-perms": self._flush_perms,
            "btn-flush-apikeys": self._flush_apikeys,
            "btn-flush-all": self._flush_all,
            "btn-list-weaviate": self._list_weaviate,
        }
        fn = actions.get(event.button.id or "")
        if fn:
            fn()

    def _log(self, msg: str) -> None:
        try:
            self.query_one("#maint-log", LogViewer).append(msg)
        except Exception:
            pass

    @work(exclusive=True)
    async def _flush_perms(self) -> None:
        from admin_console.services.maintenance_service import flush_permissions_cache  # noqa: PLC0415

        count, err = await flush_permissions_cache()
        if err:
            self._log(f"ERROR flush perms: {err}")
        else:
            self._log(f"Flushed {count} permission cache keys")
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("cache_flush_perms", "redis", {"count": count})

    @work(exclusive=True)
    async def _flush_apikeys(self) -> None:
        from admin_console.services.maintenance_service import flush_apikeys_cache  # noqa: PLC0415

        count, err = await flush_apikeys_cache()
        if err:
            self._log(f"ERROR flush apikeys: {err}")
        else:
            self._log(f"Flushed {count} API key cache entries")
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("cache_flush_apikeys", "redis", {"count": count})

    @work(exclusive=True)
    async def _flush_all(self) -> None:
        confirmed = await self.app.push_screen_wait(ConfirmDialog("Flush ALL of Redis DB 0?"))
        if not confirmed:
            return
        from admin_console.services.maintenance_service import flush_all_cache  # noqa: PLC0415

        ok, err = await flush_all_cache(db_index=0)
        if ok:
            self._log("Flushed Redis DB 0")
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("cache_flush_all", "redis:0", {})
        else:
            self._log(f"ERROR: {err}")

    @work(exclusive=True)
    async def _list_weaviate(self) -> None:
        from admin_console.services.maintenance_service import weaviate_list_collections_with_counts  # noqa: PLC0415

        try:
            cols = await weaviate_list_collections_with_counts()
            if not cols:
                self._log("Weaviate: no collections (or service unavailable)")
                return
            for col in cols:
                self._log(f"Weaviate: {col['name']} — {col['count']} objects")
        except Exception as exc:
            self._log(f"Weaviate ERROR: {exc}")
            self.notify(f"Weaviate error: {exc}", severity="error")
