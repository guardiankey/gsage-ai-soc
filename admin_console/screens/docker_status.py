"""DockerPanel — container table, log viewer, exec panel."""

from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Label

from admin_console.widgets.log_viewer import LogViewer


class DockerPanel(Widget):
    DEFAULT_CSS = """
    DockerPanel {
        height: 1fr;
        padding: 1;
    }
    DockerPanel #top-row {
        height: 14;
    }
    DockerPanel #container-table {
        width: 1fr;
        height: 14;
        border: round #555753;
        margin-right: 1;
    }
    DockerPanel #detail-col {
        width: 40;
        height: 14;
        border: round #555753;
        padding: 0 1;
        overflow-y: auto;
    }
    DockerPanel #log-section {
        height: 1fr;
        margin-top: 1;
    }
    DockerPanel #exec-row {
        height: 3;
        layout: horizontal;
    }
    DockerPanel #exec-input {
        width: 1fr;
    }
    DockerPanel Button {
        width: auto;
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-row"):
            yield DataTable(id="container-table", cursor_type="row")
            with Vertical(id="detail-col"):
                yield Label("Container detail", id="detail-label")
        with Vertical(id="log-section"):
            with Horizontal(id="exec-row"):
                yield Input(placeholder="command… (e.g. ps aux)", id="exec-input")
                yield Button("Exec", id="btn-exec", variant="default")
                yield Button("Logs", id="btn-logs", variant="default")
                yield Button("Restart", id="btn-restart", variant="warning")
                yield Button("Recreate", id="btn-recreate", variant="error")
                yield Button("Refresh", id="btn-refresh", variant="default")
            yield LogViewer(max_lines=500, id="log-viewer")

    def on_mount(self) -> None:
        table = self.query_one("#container-table", DataTable)
        table.add_columns("Container", "Image", "State", "Uptime", "Ports")
        self.load_data()

    @work(exclusive=True, thread=True)
    def load_data(self) -> None:
        import asyncio  # noqa: PLC0415

        from admin_console.db.docker_ops import docker_ps  # noqa: PLC0415

        containers = docker_ps()
        self._containers = containers

        def _update():
            table = self.query_one("#container-table", DataTable)
            table.clear()
            for c in containers:
                table.add_row(c.name, c.image[:30], c.state, c.status, c.ports[:30], key=c.name)

        self.app.call_from_thread(_update)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.load_data()
        elif event.button.id == "btn-logs":
            self._fetch_logs()
        elif event.button.id == "btn-exec":
            self._exec_command()
        elif event.button.id == "btn-restart":
            self._restart_container()
        elif event.button.id == "btn-recreate":
            self._recreate_container()

    @on(Input.Submitted, "#exec-input")
    def on_exec_input_submitted(self, event: Input.Submitted) -> None:
        """Execute command on Enter and return focus to the input."""
        self._exec_command(return_focus=True)

    @work(thread=True)
    def _fetch_logs(self) -> None:
        from admin_console.db.docker_ops import docker_logs  # noqa: PLC0415

        name = self._selected_container()
        if not name:
            self.app.call_from_thread(lambda: self.notify("Select a container first", severity="warning"))
            return
        lines = docker_logs(name)

        def _update():
            viewer = self.query_one("#log-viewer", LogViewer)
            viewer.write_lines(lines.splitlines() if lines else ["(no output)"])

        self.app.call_from_thread(_update)

    @work(thread=True)
    def _exec_command(self, return_focus: bool = False) -> None:
        from admin_console.db.docker_ops import docker_exec  # noqa: PLC0415

        name = self._selected_container()
        cmd = self.query_one("#exec-input", Input).value.strip()
        if not name or not cmd:
            self.app.call_from_thread(lambda: self.notify("Select a container and enter a command", severity="warning"))
            return
        rc, output = docker_exec(name, cmd)

        def _update():
            viewer = self.query_one("#log-viewer", LogViewer)
            viewer.write_lines(output.splitlines() if output else ["(no output)"])
            inp = self.query_one("#exec-input", Input)
            inp.value = ""
            if return_focus:
                inp.focus()

        self.app.call_from_thread(_update)

    def _selected_container(self) -> str | None:
        table = self.query_one("#container-table", DataTable)
        try:
            key = table.get_row_at(table.cursor_row)[0]
            return str(key)
        except Exception:
            return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._show_detail(str(event.row_key.value))

    @work(thread=True)
    def _restart_container(self) -> None:
        from admin_console.db.docker_ops import docker_restart  # noqa: PLC0415

        name = self._selected_container()
        if not name:
            self.app.call_from_thread(lambda: self.notify("Select a container first", severity="warning"))
            return
        rc, out = docker_restart(name)

        def _update():
            viewer = self.query_one("#log-viewer", LogViewer)
            msg = f"restart {name}: OK" if rc == 0 else f"restart failed: {out[:120]}"
            viewer.write_lines([msg])
            if rc == 0:
                self.notify(f"{name} restarted", severity="information")
            else:
                self.notify(f"Restart failed: {out[:60]}", severity="error")

        self.app.call_from_thread(_update)
        if rc == 0:
            self.app.call_from_thread(self.load_data)

    @work(exclusive=True)
    async def _recreate_container(self) -> None:
        name = self._selected_container()
        if not name:
            self.notify("Select a container first", severity="warning")
            return
        from admin_console.widgets.quick_confirm_dialog import QuickConfirmDialog  # noqa: PLC0415

        confirmed = await self.app.push_screen_wait(
            QuickConfirmDialog(f"Force-recreate container '{name}'?")
        )
        if not confirmed:
            return
        self._do_recreate(name)

    @work(thread=True)
    def _do_recreate(self, name: str) -> None:
        from admin_console.db.docker_ops import docker_recreate  # noqa: PLC0415

        rc, out = docker_recreate(name)

        def _update():
            viewer = self.query_one("#log-viewer", LogViewer)
            viewer.write_lines((out or "(no output)").splitlines()[-20:])
            if rc == 0:
                self.notify(f"{name} recreated", severity="information")
            else:
                self.notify(f"Recreate failed: {out[:60]}", severity="error")

        self.app.call_from_thread(_update)
        self.app.call_from_thread(self.load_data)

    @work(thread=True)
    def _show_detail(self, name: str) -> None:
        from admin_console.db.docker_ops import docker_inspect  # noqa: PLC0415

        info = docker_inspect(name)

        def _update():
            lbl = self.query_one("#detail-label", Label)
            lbl.update(info[:2000] if info else name)

        self.app.call_from_thread(_update)
