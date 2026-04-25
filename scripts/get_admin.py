#!/usr/bin/env python3
"""Get (or create) the bootstrap admin API key.

Workflow
--------
1. Search *docker compose logs backend* for a previously printed raw key.
2. If found → print it.
3. If not found → connect to the database.
   a. Admin user doesn't exist → create seed (ensure_admin) → print new key.
   b. Admin user exists but raw key is gone → rotate key (revoke old,
      create new) → print new key.

Usage::

    python scripts/get_admin.py
"""

from __future__ import annotations

import asyncio
import argparse
import os
import re
import secrets
import string
import subprocess
import sys
import textwrap
from typing import Optional

# Ensure repo root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.shared.bootstrap import ensure_admin, get_admin_info, reset_admin_password, rotate_admin_key
from src.shared.database import _get_session_maker

_PASSWORD_CHARS = string.ascii_letters + string.digits + "!@#$%^&*"


def _make_password(length: int = 20) -> str:
    """Generate a cryptographically secure random password."""
    return "".join(secrets.choice(_PASSWORD_CHARS) for _ in range(length))

_DIVIDER = "=" * 70
# Matches gk_live_<base64url> or gk_test_<base64url>
_KEY_PATTERN = re.compile(r"gk_(?:live|test)_[A-Za-z0-9_\-]{30,}")


# ---------------------------------------------------------------------------
# Step 1 — Docker logs scan
# ---------------------------------------------------------------------------

def _search_docker_logs() -> Optional[str]:
    """Return the first admin API key found in docker compose backend logs."""
    try:
        result = subprocess.run(
            ["docker", "compose", "logs", "backend_api", "--no-log-prefix"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout + result.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    match = _KEY_PATTERN.search(output)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# Step 2 — Database operations
# ---------------------------------------------------------------------------

async def _db_reset_password(new_password: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Reset the admin password and return (success, org_id, dept_id)."""
    from src.shared.config.settings import get_settings
    async with _get_session_maker()() as session:
        ok = await reset_admin_password(session, new_password)
        if not ok:
            return False, None, None
        info = await get_admin_info(session)
        if info:
            return True, info.get("org_id"), info.get("dept_id")
        return True, None, None

async def _db_get_or_create() -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Return (raw_key, org_id, generated_password, dept_id).

    generated_password is set only when the admin was freshly created with an
    auto-generated password (ADMIN_PASSWORD was empty or unset).
    Both raw_key and generated_password can be None if bootstrap is disabled.
    """
    from src.shared.config.settings import get_settings

    # If ADMIN_PASSWORD is not set, generate one and inject it before the
    # settings singleton is first accessed inside ensure_admin.
    generated_password: Optional[str] = None
    settings = get_settings()
    if not settings.admin_password:
        generated_password = _make_password()
        settings.admin_password = generated_password

    async with _get_session_maker()() as session:
        raw_key = await ensure_admin(session)
        if raw_key:
            # Newly seeded — fetch org_id + dept_id from info
            info = await get_admin_info(session)
            if info:
                return raw_key, info.get("org_id"), generated_password, info.get("dept_id")
            return raw_key, None, generated_password, None

        # Admin already exists — rotate to produce a new recoverable key
        result = await rotate_admin_key(session)
        if result:
            raw_key, org_id = result
            # Fetch dept_id for the rotated key scenario
            info = await get_admin_info(session)
            dept_id = info.get("dept_id") if info else None
            # Password was set at creation time; do not regenerate on rotation
            return raw_key, org_id, None, dept_id

    return None, None, None, None


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _print_key(
    raw_key: str,
    org_id: Optional[str],
    source: str,
    password: Optional[str] = None,
    dept_id: Optional[str] = None,
) -> None:
    api_host = os.getenv("GSAGE_API_HOST", "http://localhost:8000")
    print()
    print(_DIVIDER)
    print(f"  BOOTSTRAP ADMIN API KEY  [{source}]")
    print(_DIVIDER)
    print(f"  {raw_key}")
    if password:
        print()
        print("  [SENHA GERADA AUTOMATICAMENTE — guarde em lugar seguro]")
        print(f"  Senha: {password}")
    print(_DIVIDER)
    print()
    print("  Para usar o CLI client, exporte as variáveis:")
    print()
    if password:
        print(
            textwrap.dedent(
                f"""\
                export GSAGE_ADMIN_PASSWORD="{password}"
                """
            )
        )
    dept_line = f'\nexport GSAGE_DEPT_ID="{dept_id}"' if dept_id else ""
    print(
        textwrap.dedent(
            f"""\
            export GSAGE_API_KEY="{raw_key}"
            export GSAGE_ORG_ID="{org_id or '<cole aqui o org_id>'}"{dept_line}
            export GSAGE_API_HOST="{api_host}"
            """
        )
    )
    print("  Depois inicie o CLI:")
    print()
    print("    python -m cli_client")
    print()
    if org_id:
        print("  Ou em uma linha:")
        print()
        dept_inline = f' GSAGE_DEPT_ID="{dept_id}"' if dept_id else ""
        print(
            f'    GSAGE_API_KEY="{raw_key}" '
            f'GSAGE_ORG_ID="{org_id}"{dept_inline} '
            f'GSAGE_API_HOST="{api_host}" '
            "python -m cli_client"
        )
        print()
    print(_DIVIDER)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Obtém ou cria a API key do admin bootstrap."
    )
    parser.add_argument(
        "--reset-password",
        metavar="NOVA_SENHA",
        nargs="?",
        const="__generate__",
        help=(
            "Redefine a senha do admin. "
            "Sem valor → gera uma senha aleatória segura. "
            "Com valor → usa a senha fornecida."
        ),
    )
    args = parser.parse_args()

    # ── Reset password mode ──────────────────────────────────────────────
    if args.reset_password is not None:
        if args.reset_password == "__generate__":
            new_password = _make_password()
            auto_generated = True
        else:
            new_password = args.reset_password
            auto_generated = False

        print("Redefinindo senha do admin…")
        ok, org_id, dept_id = asyncio.run(_db_reset_password(new_password))
        if not ok:
            print(
                "\n[ERRO] Admin não encontrado ou ADMIN_EMAIL não está definido.\n"
            )
            sys.exit(1)

        api_host = os.getenv("GSAGE_API_HOST", "http://localhost:8000")
        print()
        print(_DIVIDER)
        print("  SENHA DO ADMIN REDEFINIDA")
        print(_DIVIDER)
        if auto_generated:
            print("  [SENHA GERADA AUTOMATICAMENTE — guarde em lugar seguro]")
        print(f"  Senha: {new_password}")
        print(_DIVIDER)
        print()
        export_lines = []
        if auto_generated:
            export_lines.append(f'export GSAGE_ADMIN_PASSWORD="{new_password}"')
        if org_id:
            export_lines.append(f'export GSAGE_ORG_ID="{org_id}"')
        if dept_id:
            export_lines.append(f'export GSAGE_DEPT_ID="{dept_id}"')
        if org_id or auto_generated:
            export_lines.append(f'export GSAGE_API_HOST="{api_host}"')
        if export_lines:
            print("  Para exportar as variáveis:")
            print()
            for line in export_lines:
                print(f"  {line}")
            print()
        print(_DIVIDER)
        print()
        return

    # ── Normal mode (get/create API key) ─────────────────────────────────
    print("Procurando API key nos logs do docker compose…")
    raw_key = _search_docker_logs()
    if raw_key:
        print("Chave encontrada nos logs.")
        # Try to get org_id + dept_id from DB without altering anything
        org_id: Optional[str] = None
        dept_id: Optional[str] = None
        try:
            async def _get_info() -> Optional[dict]:
                async with _get_session_maker()() as session:
                    return await get_admin_info(session)
            info = asyncio.run(_get_info())
            if info:
                org_id = info.get("org_id")
                dept_id = info.get("dept_id")
        except Exception:
            pass
        _print_key(raw_key, org_id, source="logs do docker", dept_id=dept_id)
        return

    print("Chave não encontrada nos logs. Conectando ao banco de dados…")
    raw_key, org_id, generated_password, dept_id = asyncio.run(_db_get_or_create())

    if raw_key:
        _print_key(raw_key, org_id, source="gerada/rotacionada agora", password=generated_password, dept_id=dept_id)
        return

    print(
        "\n[AVISO] Bootstrap desabilitado — ADMIN_EMAIL não está definido no ambiente.\n"
        "Defina ADMIN_EMAIL, ADMIN_PASSWORD (e opcionalmente ADMIN_ORG_NAME) "
        "no .env e reinicie o backend.\n"
    )


if __name__ == "__main__":
    main()

