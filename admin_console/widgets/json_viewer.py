"""JsonViewer — collapsible JSON viewer using a Textual Tree."""

from __future__ import annotations

import json
from typing import Any

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Tree


class JsonViewer(Widget):
    """Renders a JSON value as an expandable/collapsible tree.

    Usage::

        viewer = JsonViewer(title="Agent Run Result")
        viewer.load({"key": "value", "nested": {"a": 1}})
    """

    DEFAULT_CSS = """
    JsonViewer {
        height: 1fr;
        overflow-y: auto;
        border: round #555753;
    }
    JsonViewer Tree {
        background: #2e3436;
    }
    """

    def __init__(
        self,
        data: Any = None,
        title: str = "JSON",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._data = data
        self._title = title

    def compose(self) -> ComposeResult:
        tree: Tree[None] = Tree(self._title, id="json-tree")
        tree.root.expand()
        yield tree

    def on_mount(self) -> None:
        if self._data is not None:
            self.load(self._data)

    def load(self, data: Any, title: str | None = None) -> None:
        """Replace current content with *data*."""
        if title is not None:
            self._title = title
        self._data = data
        try:
            tree = self.query_one("#json-tree", Tree)
            tree.clear()
            tree.root.set_label(self._title)
            self._build_node(tree.root, data)
            tree.root.expand()
        except Exception:
            pass

    def load_text(self, text: str, title: str | None = None) -> None:
        """Parse *text* as JSON then load."""
        try:
            self.load(json.loads(text), title=title)
        except json.JSONDecodeError:
            self.load({"_raw": text}, title=title)

    def clear(self) -> None:
        try:
            tree = self.query_one("#json-tree", Tree)
            tree.clear()
            tree.root.set_label(self._title)
        except Exception:
            pass

    # ── internal ──────────────────────────────────────────────────────────────

    def _build_node(self, node: Any, value: Any, key: str | None = None) -> None:
        label_prefix = f"[bold #729fcf]{key}[/]: " if key is not None else ""

        if isinstance(value, dict):
            label = f"{label_prefix}{{...}}" if value else f"{label_prefix}{{}}"
            branch = node.add(label, expand=True)
            for k, v in value.items():
                self._build_node(branch, v, key=k)
        elif isinstance(value, list):
            label = f"{label_prefix}[...] ({len(value)})"
            branch = node.add(label, expand=True)
            for i, v in enumerate(value):
                self._build_node(branch, v, key=str(i))
        else:
            colour = "#8ae234" if isinstance(value, bool) else (
                "#fcaf3e" if isinstance(value, (int, float)) else (
                    "#ef2929" if value is None else "#eeeeec"
                )
            )
            display = "null" if value is None else repr(value)
            node.add_leaf(f"{label_prefix}[{colour}]{display}[/]")
