"""SidebarTree — navigation Tree widget for the admin console."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Tree


# Map page IDs to categories so the tree can be built declaratively
_NAVIGATION: list[tuple[str, list[tuple[str, str]]]] = [
    ("Overview", [
        ("Dashboard", "dashboard"),
        ("Docker", "docker"),
    ]),
    ("Organizations", [
        ("Organizations", "orgs"),
        ("Departments", "departments"),
        ("Users", "users"),
        ("Groups", "groups"),
        ("API Keys", "api_keys"),
        ("Approval Rules", "approval_rules"),
    ]),
    ("Tools & Interfaces", [
        ("Tool Configs", "tool_configs"),
        ("Interface Profiles", "interface_profiles"),
    ]),
    ("Data", [
        ("Sessions", "sessions"),
        ("DataStores", "datastores"),
        ("Knowledge Base", "knowledge"),
        ("Files", "files"),
        ("Email Accounts", "email_accounts"),
    ]),
    ("Jobs", [
        ("Sched. Jobs & AI Agents", "scheduled_jobs"),
        ("Background Tasks", "background_tasks"),
    ]),
    ("Infrastructure", [
        ("Redis", "redis"),
        ("Elasticsearch", "elasticsearch"),
        ("Settings", "settings"),
    ]),
    ("Maintenance", [
        ("Maintenance", "maintenance"),
    ]),
]


class SidebarTree(Widget):
    """Sidebar navigation using a Textual Tree widget."""

    DEFAULT_CSS = """
    SidebarTree {
        width: 26;
        background: #1e2426;
        border-right: solid #555753;
        overflow-y: auto;
    }
    SidebarTree Tree {
        background: #1e2426;
        padding: 0;
    }
    SidebarTree Tree > .tree--guides {
        color: #555753;
    }
    """

    class PageSelected(Message):
        """Posted when the user selects a navigation item."""

        def __init__(self, page: str) -> None:
            super().__init__()
            self.page = page

    def compose(self) -> ComposeResult:
        tree: Tree[dict] = Tree("Navigation", id="nav-tree")
        tree.root.expand()

        for section, items in _NAVIGATION:
            branch = tree.root.add(section, expand=True)
            for label, page_id in items:
                branch.add_leaf(label, data={"page": page_id})

        yield tree

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        event.stop()
        if event.node.data and "page" in event.node.data:
            self.post_message(self.PageSelected(event.node.data["page"]))
