"""StatusBadge — coloured inline status indicator."""

from __future__ import annotations

from textual.widget import Widget
from textual.reactive import reactive


class StatusBadge(Widget):
    """Renders a coloured status pill: ok/warning/error/unknown.

    Usage::

        yield StatusBadge("ok")
        yield StatusBadge("error", id="pg-badge")
        badge.status = "warning"
    """

    DEFAULT_CSS = """
    StatusBadge {
        width: auto;
        height: 1;
        padding: 0 1;
        content-align: center middle;
    }
    StatusBadge.-ok      { background: #4e9a06; color: #d3d7cf; }
    StatusBadge.-warning { background: #c4a000; color: #2e3436; }
    StatusBadge.-error   { background: #cc0000; color: #eeeeec; }
    StatusBadge.-unknown { background: #555753; color: #babdb6; }
    """

    _LABELS: dict[str, str] = {
        "ok": " OK ",
        "warning": " WARN ",
        "error": " ERR ",
        "unknown": " ? ",
    }

    status: reactive[str] = reactive("unknown")

    def __init__(
        self,
        status: str = "unknown",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.status = status

    def watch_status(self, value: str) -> None:
        self.remove_class("-ok", "-warning", "-error", "-unknown")
        css_class = value if value in ("ok", "warning", "error") else "unknown"
        self.add_class(f"-{css_class}")

    def render(self) -> str:
        return self._LABELS.get(self.status, f" {self.status.upper()} ")
