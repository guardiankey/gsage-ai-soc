"""OrgHeader — strip that displays the currently active organization."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label


class OrgHeader(Widget):
    """A single-row bar showing the active org name and a change hint.

    Reacts to ``app.active_org_name`` automatically.
    """

    DEFAULT_CSS = """
    OrgHeader {
        height: 1;
        background: #1e2426;
        color: #729fcf;
        padding: 0 2;
        dock: top;
        layout: horizontal;
    }
    OrgHeader #gsage-title {
        width: 1fr;
        color: #eeeeec;
    }
    OrgHeader #gsage-org {
        width: auto;
        color: #8ae234;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("gSage Admin Console", id="gsage-title")
        yield Label("Org: (none) — [F3] change", id="gsage-org")

    def on_mount(self) -> None:
        self._refresh_org(self.app.active_org_name)  # type: ignore[attr-defined]

    def watch_app_active_org_name(self, name: str) -> None:  # auto-watch app reactives
        self._refresh_org(name)

    def _refresh_org(self, name: str) -> None:
        try:
            lbl = self.query_one("#gsage-org", Label)
            display = name or "(none)"
            lbl.update(f"Org: {display} — [F3] change")
        except Exception:
            pass
