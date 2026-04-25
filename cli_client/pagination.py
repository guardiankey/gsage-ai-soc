"""gSage AI — CLI pagination helpers.

Provides a generic ``render_paginated_table`` that wraps any Rich Table
with a standardised footer showing page / total information.

Usage::

    from cli_client.pagination import render_paginated_table

    def _build_knowledge_table(items: list[dict], data: dict) -> Table:
        table = Table(title=f"Knowledge Base  ({data['total']} total)")
        table.add_column("ID", style="bright_cyan", no_wrap=True)
        table.add_column("Name")
        for item in items:
            table.add_row(item["id"], item.get("name") or "-")
        return table

    data = client.list_knowledge(page=page, limit=limit)
    render_paginated_table(console, data, _build_knowledge_table)
"""

from __future__ import annotations

from typing import Any, Callable

from rich.console import Console
from rich.table import Table

# Tango colours reused from commands.py
COLOR_INFO = "bright_yellow"
COLOR_DIM = "dim"
COLOR_ERROR = "bright_red"


def render_paginated_table(
    console: Console,
    data: dict[str, Any],
    build_table_fn: Callable[[list[dict], dict], Table],
    *,
    command_hint: str | None = None,
    fetch_fn: Callable[[int], dict] | None = None,
) -> None:
    """Render a Rich Table followed by a standard pagination footer.

    The ``data`` dict must conform to the ``PaginatedResponse`` envelope::

        {
            "items": [...],
            "total": 42,
            "page": 2,
            "limit": 20,
            "has_more": true,
        }

    Args:
        console:        Rich Console instance.
        data:           PaginatedResponse dict from the API.
        build_table_fn: Callable that receives ``(items, data)`` and returns a
                        ``rich.table.Table``.  The caller is responsible for all
                        column / row definitions.
        command_hint:   Optional CLI command prefix (e.g. ``"knowledge list"``)
                        used to display navigable command examples in the footer.
        fetch_fn:       Optional callable ``(page: int) -> dict`` that fetches a
                        new page from the API.  When provided, an interactive
                        prompt (n/p/q) is shown after each page so the user can
                        navigate without re-typing the command.
    """
    while True:
        items: list[dict] = data.get("items", [])
        total: int = data.get("total", len(items))
        page: int = data.get("page", 1)
        limit: int = data.get("limit", len(items) or 20)
        has_more: bool = data.get("has_more", False)

        total_pages = max(1, (total + limit - 1) // limit)

        table = build_table_fn(items, data)
        console.print(table)

        # --- Footer ---
        page_info = f"Page {page}/{total_pages}"
        total_info = f"{total} total"

        nav_hints: list[str] = []
        if page > 1:
            if command_hint:
                nav_hints.append(f"prev: [bright_cyan]{command_hint} {page - 1}[/bright_cyan]")
            else:
                nav_hints.append(f"prev: [bright_cyan]{page - 1}[/bright_cyan]")
        if has_more:
            if command_hint:
                nav_hints.append(f"next: [bright_cyan]{command_hint} {page + 1}[/bright_cyan]")
            else:
                nav_hints.append(f"next: [bright_cyan]{page + 1}[/bright_cyan]")

        nav_str = "  ·  " + "  ·  ".join(nav_hints) if nav_hints else ""
        console.print(
            f"[{COLOR_DIM}]{page_info}  ·  {total_info}{nav_str}[/{COLOR_DIM}]"
        )

        # --- Interactive navigation (only when fetch_fn is provided and there are pages to navigate) ---
        if fetch_fn is None or (not has_more and page <= 1):
            break

        prompt_parts: list[str] = []
        options: dict[str, int] = {}
        if has_more:
            prompt_parts.append("[bright_cyan]\\[n][/bright_cyan]ext")
            options["n"] = page + 1
        if page > 1:
            prompt_parts.append("[bright_cyan]\\[p][/bright_cyan]rev")
            options["p"] = page - 1
        prompt_parts.append("[bright_cyan]\\[q][/bright_cyan]uit")

        prompt_str = f"[{COLOR_DIM}]" + f"  ·  ".join(prompt_parts) + f"[/{COLOR_DIM}]  ❯  "
        try:
            choice = console.input(prompt_str).strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print()
            break

        if choice not in options:
            break

        try:
            data = fetch_fn(options[choice])
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to fetch page {options[choice]}: {exc}[/{COLOR_ERROR}]")
            break
