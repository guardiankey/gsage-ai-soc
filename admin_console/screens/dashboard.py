"""DashboardPanel — service status grid + quick stats + last 5 agent runs."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Label, Static

from admin_console.widgets.status_badge import StatusBadge


class _ServiceCard(Widget):
    DEFAULT_CSS = """
    _ServiceCard {
        width: 1fr;
        height: 5;
        border: round #555753;
        padding: 0 1;
        background: #2e3436;
    }
    _ServiceCard Label#svc-name {
        color: #babdb6;
        height: 1;
    }
    _ServiceCard Label#svc-detail {
        color: #eeeeec;
        height: 1;
    }
    """

    def __init__(self, name: str, **kw) -> None:
        super().__init__(**kw)
        self._svc = name

    def compose(self) -> ComposeResult:
        yield Label(self._svc.upper(), id="svc-name")
        yield StatusBadge("unknown", id="badge")
        yield Label("…", id="svc-detail")

    def update(self, status: str, detail: str) -> None:
        try:
            self.query_one("#badge", StatusBadge).status = status
            self.query_one("#svc-detail", Label).update(detail[:60])
        except Exception:
            pass


class DashboardPanel(Widget):
    """Service health grid + last runs."""

    DEFAULT_CSS = """
    DashboardPanel {
        height: 1fr;
        padding: 1;
    }
    DashboardPanel #svc-rows {
        height: auto;
    }
    DashboardPanel #svc-grid-1,
    DashboardPanel #svc-grid-2 {
        height: auto;
        layout: horizontal;
    }
    DashboardPanel #runs-title {
        color: #729fcf;
        text-style: bold;
        height: 1;
        margin-top: 1;
    }
    DashboardPanel DataTable {
        height: 1fr;
    }
    """

    _SERVICES_ROW1 = ["postgres", "redis", "elasticsearch", "weaviate", "minio", "docker"]
    _SERVICES_ROW2 = ["backend_api", "mcp_server", "celery_workers", "celery_beat", "ollama", "frontend", "email_worker"]

    def compose(self) -> ComposeResult:
        with Vertical(id="svc-rows"):
            with Horizontal(id="svc-grid-1"):
                for svc in self._SERVICES_ROW1:
                    yield _ServiceCard(svc, id=f"card-{svc}")
            with Horizontal(id="svc-grid-2"):
                for svc in self._SERVICES_ROW2:
                    yield _ServiceCard(svc, id=f"card-{svc}")
        yield Label("Recent Agent Runs", id="runs-title")
        yield DataTable(id="runs-table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#runs-table", DataTable)
        table.add_columns("Agent", "Model", "Status", "Tokens", "Duration", "Time")
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        await self._load_health()
        await self._load_runs()

    async def _load_health(self) -> None:
        from admin_console.services.monitoring_service import get_full_health  # noqa: PLC0415

        health = await get_full_health()
        for svc, info in health.items():
            try:
                card = self.query_one(f"#card-{svc}", _ServiceCard)
                card.update(info.get("status", "unknown"), info.get("details", ""))
            except Exception:
                pass

    async def _load_runs(self) -> None:
        org_id = getattr(self.app, "active_org_id", None)
        if not org_id:
            return
        try:
            import uuid as _uuid  # noqa: PLC0415

            from admin_console.db.postgres import get_session  # noqa: PLC0415
            from admin_console.services.session_service import list_recent_runs  # noqa: PLC0415

            async with get_session() as db:
                runs = await list_recent_runs(db, _uuid.UUID(org_id), limit=5)

            table = self.query_one("#runs-table", DataTable)
            table.clear()
            for r in runs:
                input_t = r.get("input_tokens") or 0
                output_t = r.get("output_tokens") or 0
                tokens = input_t + output_t
                duration_ms = r.get("duration_ms")
                elapsed = duration_ms / 1000 if duration_ms is not None else None
                table.add_row(
                    r.get("agent_type", "—"),
                    "—",
                    r.get("status", "—"),
                    str(tokens) if tokens else "—",
                    f"{elapsed:.1f}s" if elapsed is not None else "—",
                    (r.get("created_at") or "")[:19],
                )
        except Exception as exc:
            self.notify(f"Runs: {exc}", severity="error")
