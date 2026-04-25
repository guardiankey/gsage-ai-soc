"""LogViewer — scrollable tail of log lines using RichLog."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog


class LogViewer(Widget):
    """Wraps :class:`RichLog` with helpers for loading plain text lines.

    Usage::

        viewer = LogViewer(max_lines=500, title="Container Logs")
        viewer.write_lines(["line1", "line2", ...])
        viewer.clear()
    """

    DEFAULT_CSS = """
    LogViewer {
        height: 1fr;
        border: round #555753;
        overflow: hidden;
    }
    LogViewer RichLog {
        height: 1fr;
        background: #1e2426;
    }
    """

    def __init__(
        self,
        max_lines: int = 500,
        title: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._max_lines = max_lines
        self._title = title

    def compose(self) -> ComposeResult:
        log = RichLog(highlight=True, markup=True, max_lines=self._max_lines, id="rich-log")
        if self._title:
            log.border_title = self._title
        yield log

    def write_lines(self, lines: list[str]) -> None:
        log = self.query_one("#rich-log", RichLog)
        log.clear()
        for line in lines:
            log.write(line)

    def append(self, line: str) -> None:
        self.query_one("#rich-log", RichLog).write(line)

    def write_text(self, text: str) -> None:
        self.write_lines(text.splitlines())

    def clear(self) -> None:
        self.query_one("#rich-log", RichLog).clear()
