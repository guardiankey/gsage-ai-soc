"""Entry point: ``python -m ops_cli …``."""

from __future__ import annotations

from src.ops_cli.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
