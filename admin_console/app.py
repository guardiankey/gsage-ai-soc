"""gSage Admin Console — main Textual application."""

from __future__ import annotations

import logging
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.notifications import SeverityLevel
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import ContentSwitcher, Footer, Label

from admin_console.screens.api_keys import ApiKeysPanel
from admin_console.screens.approval_rules import ApprovalRulesPanel
from admin_console.screens.background_tasks import BackgroundTasksPanel
from admin_console.screens.dashboard import DashboardPanel
from admin_console.screens.datastores_browse import DatastoresBrowsePanel
from admin_console.screens.docker_status import DockerPanel
from admin_console.screens.email_accounts import EmailAccountsPanel
from admin_console.screens.es_inspect import EsInspectPanel
from admin_console.screens.files_browse import FilesBrowsePanel
from admin_console.screens.group_manage import GroupManagePanel
from admin_console.screens.interface_profiles import InterfaceProfilesPanel
from admin_console.screens.knowledge_browse import KnowledgeBrowsePanel
from admin_console.screens.maintenance import MaintenancePanel
from admin_console.screens.dept_manage import DeptManagePanel
from admin_console.screens.org_manage import OrgManagePanel
from admin_console.screens.org_selector import OrgSelectorModal
from admin_console.screens.dept_selector import DeptSelectorModal
from admin_console.screens.redis_inspect import RedisInspectPanel
from admin_console.screens.scheduled_jobs import ScheduledJobsPanel
from admin_console.screens.sessions_browse import SessionsBrowsePanel
from admin_console.screens.settings_view import SettingsViewPanel
from admin_console.screens.tool_config import ToolConfigPanel
from admin_console.screens.user_manage import UserManagePanel
from admin_console.widgets.sidebar_tree import SidebarTree

_CSS_PATH = str(Path(__file__).parent / "css" / "admin.tcss")


class AdminApp(App[None]):
    """gSage TUI Admin Console."""

    TITLE = "gSage Admin Console"
    CSS_PATH = [_CSS_PATH]

    def __init__(self, debug: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self._debug = debug
        self._debug_logger: logging.Logger | None = None
        if debug:
            log_path = Path.home() / ".gsage_ai" / "admin_debug.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(str(log_path), encoding="utf-8")
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
            )
            logger = logging.getLogger("gsage_admin_debug")
            logger.setLevel(logging.DEBUG)
            logger.addHandler(handler)
            self._debug_logger = logger

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
    ) -> None:
        if self._debug and self._debug_logger:
            _SEVERITY_TO_LEVEL = {
                "error": logging.ERROR,
                "warning": logging.WARNING,
            }
            level = _SEVERITY_TO_LEVEL.get(severity, logging.INFO)
            self._debug_logger.log(level, "[%s] %s", severity, message)
        super().notify(message, title=title, severity=severity, timeout=timeout)

    BINDINGS = [
        Binding("f1", "help", "Help", show=True),
        Binding("f2", "goto('dashboard')", "Dashboard", show=True),
        Binding("f3", "change_org", "Change Org", show=True),
        Binding("f4", "change_dept", "Change Dept", show=True),
        Binding("f5", "refresh_panel", "Refresh", show=True),
        Binding("ctrl+c", "quit", "Quit", show=True, priority=True),
    ]

    active_org_id: reactive[str | None] = reactive(None)
    active_org_name: reactive[str] = reactive("(none)")
    active_dept_id: reactive[str | None] = reactive(None)
    active_dept_name: reactive[str] = reactive("(none)")

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Label("", id="org-bar")
        with Horizontal(id="main"):
            yield SidebarTree(id="sidebar")
            with ContentSwitcher(initial="dashboard", id="content"):
                yield DashboardPanel(id="dashboard")
                yield DockerPanel(id="docker")
                yield OrgManagePanel(id="orgs")
                yield DeptManagePanel(id="departments")
                yield UserManagePanel(id="users")
                yield GroupManagePanel(id="groups")
                yield ApiKeysPanel(id="api_keys")
                yield ApprovalRulesPanel(id="approval_rules")
                yield ToolConfigPanel(id="tool_configs")
                yield InterfaceProfilesPanel(id="interface_profiles")
                yield SessionsBrowsePanel(id="sessions")
                yield DatastoresBrowsePanel(id="datastores")
                yield KnowledgeBrowsePanel(id="knowledge")
                yield FilesBrowsePanel(id="files")
                yield EmailAccountsPanel(id="email_accounts")
                yield ScheduledJobsPanel(id="scheduled_jobs")
                yield BackgroundTasksPanel(id="background_tasks")
                yield RedisInspectPanel(id="redis")
                yield EsInspectPanel(id="elasticsearch")
                yield SettingsViewPanel(id="settings")
                yield MaintenancePanel(id="maintenance")
        yield Footer()

    def on_mount(self) -> None:
        self._update_org_bar()

    async def on_unmount(self) -> None:
        """Dispose async DB pool before the event loop closes.

        Without this, asyncpg connections held by the SQLAlchemy pool are
        garbage-collected after the loop is gone, producing noisy
        ResourceWarning messages on stderr.
        """
        try:
            from src.shared.database import dispose_engine_pool  # noqa: PLC0415

            await dispose_engine_pool()
        except Exception:
            pass

    # ── Reactives ─────────────────────────────────────────────────────────────

    def watch_active_org_name(self, name: str) -> None:
        self._update_org_bar()

    def watch_active_dept_name(self, name: str) -> None:
        self._update_org_bar()

    def watch_active_org_id(self, _: str | None) -> None:
        self._refresh_context_panels()

    def watch_active_dept_id(self, _: str | None) -> None:
        self._refresh_context_panels()

    def _refresh_context_panels(self) -> None:
        """Call load_data() on all panels that react to org/dept context."""
        try:
            for child in self.query_one(ContentSwitcher).children:
                if hasattr(child, "load_data"):
                    child.load_data()  # type: ignore[attr-defined]
        except Exception:
            pass

    def _update_org_bar(self) -> None:
        try:
            org_name = self.active_org_name or "(none)"
            dept_name = self.active_dept_name or "(none)"
            self.query_one("#org-bar", Label).update(
                f"[bold #eeeeec]gSage Admin Console[/]  "
                f"[#8ae234]Org: {org_name}[/]  "
                f"[#729fcf]Dept: {dept_name}[/]  "
                f"[[#babdb6]F3[/] org | [#babdb6]F4[/] dept]"
            )
        except Exception:
            pass

    # ── Sidebar navigation ────────────────────────────────────────────────────

    def on_sidebar_tree_page_selected(self, event: SidebarTree.PageSelected) -> None:
        self._switch_to(event.page)

    def _switch_to(self, page: str) -> None:
        try:
            self.query_one(ContentSwitcher).current = page
        except Exception:
            pass

    # ── Bindings / Actions ────────────────────────────────────────────────────

    def action_goto(self, page: str) -> None:
        self._switch_to(page)

    def action_change_org(self) -> None:
        self._do_change_org()

    @work(exclusive=True)
    async def _do_change_org(self) -> None:
        org_id = await self.push_screen_wait(OrgSelectorModal())
        if org_id:
            # Clear active dept when changing org
            self.active_dept_id = None
            self.active_dept_name = "(none)"
            await self._set_org(org_id)

    async def _set_org(self, org_id: str) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.org_service import get_org  # noqa: PLC0415
        import uuid  # noqa: PLC0415

        try:
            async with get_session() as db:
                org = await get_org(db, uuid.UUID(org_id))
            self.active_org_id = org_id
            self.active_org_name = org["name"] if org else org_id[:8]
            self.notify(f"Active org: {self.active_org_name}")
        except Exception as exc:
            self.active_org_id = org_id
            self.active_org_name = org_id[:8]
            self.notify(f"Org set (detail error: {exc})", severity="warning")

    def action_change_dept(self) -> None:
        self._do_change_dept()

    @work(exclusive=True)
    async def _do_change_dept(self) -> None:
        if not self.active_org_id:
            self.notify("Select an organization first (F3)", severity="warning")
            return
        dept_id = await self.push_screen_wait(DeptSelectorModal(self.active_org_id))
        if dept_id:
            await self._set_dept(dept_id)

    async def _set_dept(self, dept_id: str) -> None:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from admin_console.services.dept_service import get_dept  # noqa: PLC0415
        import uuid  # noqa: PLC0415

        try:
            async with get_session() as db:
                dept = await get_dept(db, uuid.UUID(dept_id))
            self.active_dept_id = dept_id
            self.active_dept_name = dept["name"] if dept else dept_id[:8]
            self.notify(f"Active dept: {self.active_dept_name}")
        except Exception as exc:
            self.active_dept_id = dept_id
            self.active_dept_name = dept_id[:8]
            self.notify(f"Dept set (detail error: {exc})", severity="warning")

    def action_refresh_panel(self) -> None:
        try:
            current_id = self.query_one(ContentSwitcher).current
            panel = self.query_one(f"#{current_id}")
            if hasattr(panel, "load_data"):
                panel.load_data()  # type: ignore[attr-defined]
            self.notify("Refreshed")
        except Exception:
            pass

    def action_help(self) -> None:
        self.notify(
            "F2: Dashboard  F3: Change Org  F5: Refresh  Ctrl+C: Quit",
            title="Keyboard Shortcuts",
            timeout=8,
        )
