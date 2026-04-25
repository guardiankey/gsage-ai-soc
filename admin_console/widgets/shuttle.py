"""ShuttleWidget — dual-listbox with keyboard navigation and multi-select."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Label, ListView, ListItem


class ShuttleWidget(Widget):
    """Two-pane selector: left=available, right=assigned.

    Keyboard shortcuts (when a list pane has focus):
        - ↑ / ↓        : navigate items
        - Enter / →     : move highlighted/selected item(s) → assigned
        - ←             : move highlighted/selected item(s) → available
        - Space         : toggle current item into/out of the multi-selection
        - Shift+↑ / ↓   : extend multi-selection upward / downward

    Buttons:  [>] move selected right  [<] move selected left
              [>>] move all right      [<<] move all left

    Multi-selection is tracked by item VALUE (not display index) so it
    survives re-sorts after moves.  Selected items are highlighted with the
    ``multi-selected`` CSS class.

    Items are (label, value) tuples.  Values must be unique within each pane.

    Usage::

        shuttle = ShuttleWidget(
            available=[("Read", "read"), ("Write", "write")],
            assigned=[("Admin", "admin")],
        )
        shuttle.get_assigned()  # → ["admin"]

    Listens to :class:`ShuttleWidget.Changed` message.
    """

    DEFAULT_CSS = """
    ShuttleWidget {
        height: 1fr;
        layout: horizontal;
    }
    ShuttleWidget .shuttle-pane {
        width: 1fr;
        max-width: 40;
        border: round #555753;
        height: 1fr;
    }
    ShuttleWidget .shuttle-pane Label {
        text-align: center;
        background: #1e2426;
        color: #729fcf;
        height: 1;
    }
    ShuttleWidget ListView {
        height: 1fr;
        background: #2e3436;
    }
    ShuttleWidget #btn-col {
        width: 7;
        align: center middle;
        padding: 0 1;
    }
    ShuttleWidget Button {
        width: 5;
        min-width: 5;
        margin-bottom: 1;
    }
    ShuttleWidget ListItem.multi-selected {
        background: $accent 25%;
    }
    """

    class Changed(Message):
        """Posted whenever the assigned list changes."""

        def __init__(self, assigned: list[str]) -> None:
            super().__init__()
            self.assigned = assigned

    def __init__(
        self,
        available: list[tuple[str, str]] | None = None,
        assigned: list[tuple[str, str]] | None = None,
        available_label: str = "Available",
        assigned_label: str = "Assigned",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._available: list[tuple[str, str]] = list(available or [])
        self._assigned: list[tuple[str, str]] = list(assigned or [])
        self._available_label = available_label
        self._assigned_label = assigned_label
        # Multi-select tracked by VALUE strings (stable across re-sorts)
        self._sel_avail: set[str] = set()
        self._sel_assign: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(classes="shuttle-pane"):
            yield Label(self._available_label)
            yield ListView(id="lv-available")
        with Vertical(id="btn-col"):
            yield Button(">", id="btn-right", variant="default")
            yield Button("<", id="btn-left", variant="default")
            yield Button(">>", id="btn-all-right", variant="default")
            yield Button("<<", id="btn-all-left", variant="default")
        with Vertical(classes="shuttle-pane"):
            yield Label(self._assigned_label)
            yield ListView(id="lv-assigned")

    def on_mount(self) -> None:
        self._refresh_lists()

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_items(
        self,
        available: list[tuple[str, str]],
        assigned: list[tuple[str, str]],
    ) -> None:
        self._available = list(available)
        self._assigned = list(assigned)
        self._sel_avail.clear()
        self._sel_assign.clear()
        self._refresh_lists()

    def get_assigned(self) -> list[str]:
        """Return the values of assigned items."""
        return [v for _, v in self._assigned]

    # ── Buttons ────────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button.id
        if btn == "btn-right":
            self._move_pane(
                self.query_one("#lv-available", ListView),
                self._available, self._assigned, self._sel_avail,
            )
        elif btn == "btn-left":
            self._move_pane(
                self.query_one("#lv-assigned", ListView),
                self._assigned, self._available, self._sel_assign,
            )
        elif btn == "btn-all-right":
            self._assigned.extend(self._available)
            self._available.clear()
            self._sel_avail.clear()
        elif btn == "btn-all-left":
            self._available.extend(self._assigned)
            self._assigned.clear()
            self._sel_assign.clear()
        else:
            return  # not our button — do NOT stop or refresh

        self._refresh_lists()
        self.post_message(self.Changed(self.get_assigned()))
        event.stop()  # prevent bubbling to parent panels (e.g. "btn-save" collision)

    # ── Keyboard ──────────────────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter pressed on a list item — move it (and any multi-selected) to the other pane."""
        lv = event.list_view
        is_avail = lv.id == "lv-available"
        source = self._available if is_avail else self._assigned
        dest = self._assigned if is_avail else self._available
        sel = self._sel_avail if is_avail else self._sel_assign
        focus_id = lv.id

        cur_value = self._lv_current_value(lv)
        if cur_value is None:
            return

        values_to_move = set(sel) if sel else {cur_value}
        self._move_by_values(source, dest, values_to_move)
        sel.clear()

        self._refresh_lists()
        self.post_message(self.Changed(self.get_assigned()))
        self.call_after_refresh(
            lambda fid=focus_id: self.query_one(f"#{fid}", ListView).focus()
        )
        event.stop()

    def on_key(self, event: Key) -> None:
        """Handle → / ← move arrows and Space / Shift+arrows multi-select."""
        focused = self.app.focused
        if not isinstance(focused, ListView):
            return

        lv_avail = self.query_one("#lv-available", ListView)
        lv_assign = self.query_one("#lv-assigned", ListView)
        if focused not in (lv_avail, lv_assign):
            return

        is_avail = focused is lv_avail
        source = self._available if is_avail else self._assigned
        dest = self._assigned if is_avail else self._available
        sel = self._sel_avail if is_avail else self._sel_assign
        focus_id = focused.id

        if (event.key == "right" and is_avail) or (event.key == "left" and not is_avail):
            # Move highlighted / multi-selected items to opposite pane
            cur_value = self._lv_current_value(focused)
            if cur_value is None:
                return
            values_to_move = set(sel) if sel else {cur_value}
            self._move_by_values(source, dest, values_to_move)
            sel.clear()
            self._refresh_lists()
            self.post_message(self.Changed(self.get_assigned()))
            self.call_after_refresh(
                lambda fid=focus_id: self.query_one(f"#{fid}", ListView).focus()
            )
            event.prevent_default()
            event.stop()

        elif event.key == "space":
            # Toggle current item in/out of multi-selection
            cur_value = self._lv_current_value(focused)
            if cur_value is not None:
                if cur_value in sel:
                    sel.discard(cur_value)
                else:
                    sel.add(cur_value)
                self._mark_selection(focused, sel)
            event.prevent_default()
            event.stop()

        elif event.key in ("shift+down", "shift+up"):
            # Extend selection: add current + move highlight one step
            cur_value = self._lv_current_value(focused)
            if cur_value is not None:
                sel.add(cur_value)
            idx = focused.index or 0
            if event.key == "shift+down" and idx < len(source) - 1:
                focused.index = idx + 1
            elif event.key == "shift+up" and idx > 0:
                focused.index = idx - 1
            new_value = self._lv_current_value(focused)
            if new_value is not None:
                sel.add(new_value)
            self._mark_selection(focused, sel)
            event.prevent_default()
            event.stop()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _lv_current_value(self, lv: ListView) -> str | None:
        """Return the value (name attr) of the currently highlighted ListItem."""
        idx = lv.index
        if idx is None:
            return None
        children = list(lv.query("ListItem"))
        if 0 <= idx < len(children):
            return children[idx].name
        return None

    def _move_pane(
        self,
        lv: ListView,
        source: list[tuple[str, str]],
        dest: list[tuple[str, str]],
        sel: set[str],
    ) -> None:
        """Move multi-selected items (or current highlight) from source to dest."""
        if sel:
            self._move_by_values(source, dest, set(sel))
            sel.clear()
        else:
            cur_value = self._lv_current_value(lv)
            if cur_value is not None:
                self._move_by_values(source, dest, {cur_value})

    def _move_by_values(
        self,
        source: list[tuple[str, str]],
        dest: list[tuple[str, str]],
        values: set[str],
    ) -> None:
        """Move all items whose value is in *values* from source to dest."""
        to_move = [item for item in source if item[1] in values]
        for item in to_move:
            source.remove(item)
            dest.append(item)

    def _refresh_lists(self) -> None:
        lv_avail = self.query_one("#lv-available", ListView)
        lv_assign = self.query_one("#lv-assigned", ListView)

        lv_avail.clear()
        for label, value in sorted(self._available, key=lambda x: x[0]):
            lv_avail.append(ListItem(Label(label), name=value))

        lv_assign.clear()
        for label, value in sorted(self._assigned, key=lambda x: x[0]):
            lv_assign.append(ListItem(Label(label), name=value))

        # Re-apply multi-select visual markers after DOM rebuild
        if self._sel_avail:
            self.call_after_refresh(
                lambda: self._mark_selection(
                    self.query_one("#lv-available", ListView), self._sel_avail
                )
            )
        if self._sel_assign:
            self.call_after_refresh(
                lambda: self._mark_selection(
                    self.query_one("#lv-assigned", ListView), self._sel_assign
                )
            )

    def _mark_selection(self, lv: ListView, sel: set[str]) -> None:
        """Apply/remove the ``multi-selected`` CSS class on ListItems."""
        for child in lv.query("ListItem"):
            if child.name in sel:
                child.add_class("multi-selected")
            else:
                child.remove_class("multi-selected")

