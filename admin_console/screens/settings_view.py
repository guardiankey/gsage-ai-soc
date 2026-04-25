"""SettingsViewPanel — read-only settings with masked secrets + reveal toggle."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer
from textual.widget import Widget
from textual.widgets import Button

from admin_console.widgets.kv_panel import KVPanel


_SECRET_FIELDS = {
    "postgres_password",
    "redis_password",
    "minio_secret_key",
    "jwt_secret_key",
    "smtp_password",
    "gk_encryption_key",
    "gk_admin_secret",
    "llm_api_key",
    "secret_key",
}


class SettingsViewPanel(Widget):
    DEFAULT_CSS = """
    SettingsViewPanel {
        height: 1fr;
        padding: 1;
    }
    SettingsViewPanel #btn-row { height: 3; layout: horizontal; }
    SettingsViewPanel #btn-row Button { margin-right: 1; }
    SettingsViewPanel ScrollableContainer { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="btn-row"):
            yield Button("Reveal Secrets", id="btn-reveal", variant="warning")
            yield Button("Hide Secrets", id="btn-hide")
        with ScrollableContainer():
            yield KVPanel(title="Settings", id="settings-panel")

    def on_mount(self) -> None:
        self._reveal = False
        self.load_data()

    @work(exclusive=True)
    async def load_data(self) -> None:
        from admin_console.config import get_admin_settings  # noqa: PLC0415

        try:
            settings = get_admin_settings()
            data = {}
            for field_name in settings.model_fields:
                value = getattr(settings, field_name, None)
                if not self._reveal and field_name in _SECRET_FIELDS:
                    value = "****"
                data[field_name] = str(value) if value is not None else "—"

            self.query_one("#settings-panel", KVPanel).update(data)
        except Exception as exc:
            self.notify(f"Settings error: {exc}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-reveal":
            self._reveal = True
            from admin_console.audit import log_event  # noqa: PLC0415
            log_event("settings_reveal", "all", {})
            self.load_data()
        elif event.button.id == "btn-hide":
            self._reveal = False
            self.load_data()
