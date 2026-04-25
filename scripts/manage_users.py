#!/usr/bin/env python3
"""User management CLI script.

Subcommands
-----------
  create          Create a user and add them to an org
  list            List members of an org
  info            Show details for a user (orgs, groups)
  reset-password  Reset a user's password
  update          Update user attributes
  add-to-group    Add a user to a group within an org

Usage::

    python scripts/manage_users.py create --email alice@example.com --org myorg --role member
    python scripts/manage_users.py list   --org myorg
    python scripts/manage_users.py info   --email alice@example.com
    python scripts/manage_users.py reset-password --email alice@example.com [--password NOVA]
    python scripts/manage_users.py update --email alice@example.com --full-name "Alice Smith"
    python scripts/manage_users.py add-to-group --email alice@example.com --org myorg --group devs
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import string
import sys
import uuid
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.shared.database import _get_session_maker
from src.shared.models.group import GSageGroup
from src.shared.models.organization import GSageOrganization
from src.shared.models.user import GSageUser
from src.shared.models.user_organization import GSageUserOrganization
from src.shared.models.department import GSageDepartment
from src.shared.models.user_department import GSageUserDepartment
from src.shared.security.auth import hash_password

_DIVIDER = "=" * 70
_PASSWORD_CHARS = string.ascii_letters + string.digits + "!@#$%^&*"
_VALID_ROLES = ("owner", "admin", "member", "viewer")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_password(length: int = 20) -> str:
    return "".join(secrets.choice(_PASSWORD_CHARS) for _ in range(length))


def _try_uuid(value: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _resolve_org(session, org_ref: str) -> Optional[GSageOrganization]:
    """Resolve org by UUID, slug, or name (case-insensitive)."""
    uid = _try_uuid(org_ref)
    if uid:
        result = await session.execute(
            select(GSageOrganization).where(GSageOrganization.id == uid)
        )
        return result.scalar_one_or_none()

    # Try slug first, then name
    result = await session.execute(
        select(GSageOrganization).where(GSageOrganization.slug == org_ref)
    )
    org = result.scalar_one_or_none()
    if org:
        return org

    result = await session.execute(
        select(GSageOrganization).where(
            GSageOrganization.name.ilike(org_ref)
        )
    )
    return result.scalar_one_or_none()


async def _find_user(session, email: str) -> Optional[GSageUser]:
    result = await session.execute(
        select(GSageUser).where(GSageUser.email.ilike(email))
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

async def _cmd_create(args: argparse.Namespace) -> None:
    async with _get_session_maker()() as session:
        # Resolve org
        org = await _resolve_org(session, args.org)
        if not org:
            print(f"[ERRO] Organização não encontrada: {args.org!r}")
            sys.exit(1)

        role = args.role or "member"
        if role not in _VALID_ROLES:
            print(f"[ERRO] Role inválido: {role!r}. Válidos: {', '.join(_VALID_ROLES)}")
            sys.exit(1)

        # Check if user already exists
        user = await _find_user(session, args.email)
        new_user = user is None

        generated_password: Optional[str] = None
        if new_user:
            if args.password:
                raw_password = args.password
            else:
                raw_password = _make_password()
                generated_password = raw_password

            user = GSageUser(
                email=args.email.lower(),
                full_name=args.full_name or args.email.split("@")[0],
                password_hash=hash_password(raw_password),
                is_active=True,
            )
            session.add(user)
            await session.flush()  # get user.id

        # Check membership
        membership_result = await session.execute(
            select(GSageUserOrganization).where(
                GSageUserOrganization.user_id == user.id,
                GSageUserOrganization.org_id == org.id,
            )
        )
        existing_membership = membership_result.scalar_one_or_none()

        if existing_membership:
            print(f"[AVISO] Usuário {args.email!r} já é membro da org {org.name!r} (role: {existing_membership.role}).")
            if new_user:
                await session.commit()
        else:
            membership = GSageUserOrganization(
                user_id=user.id,
                org_id=org.id,
                role=role,
                is_active=True,
            )
            session.add(membership)
            await session.flush()

            # Add to Default department if it exists
            dept_result = await session.execute(
                select(GSageDepartment).where(
                    GSageDepartment.org_id == org.id,
                    GSageDepartment.is_default.is_(True),
                    GSageDepartment.is_active.is_(True),
                )
            )
            default_dept = dept_result.scalar_one_or_none()
            if default_dept:
                dept_role = "admin" if role in ("owner", "admin") else "member"
                session.add(GSageUserDepartment(
                    user_id=user.id,
                    dept_id=default_dept.id,
                    role=dept_role,
                    is_active=True,
                ))

            await session.commit()

        print()
        print(_DIVIDER)
        if new_user:
            print("  USUÁRIO CRIADO")
        else:
            print("  MEMBRO ADICIONADO À ORG")
        print(_DIVIDER)
        print(f"  Email:  {user.email}")
        print(f"  Nome:   {user.full_name}")
        print(f"  UUID:   {user.id}")
        print(f"  Org:    {org.name}  (slug: {org.slug})")
        print(f"  Role:   {role}")
        if generated_password:
            print()
            print("  [SENHA GERADA AUTOMATICAMENTE — guarde em lugar seguro]")
            print(f"  Senha:  {generated_password}")
        print(_DIVIDER)
        print()


async def _cmd_list(args: argparse.Namespace) -> None:
    async with _get_session_maker()() as session:
        org = await _resolve_org(session, args.org)
        if not org:
            print(f"[ERRO] Organização não encontrada: {args.org!r}")
            sys.exit(1)

        result = await session.execute(
            select(GSageUserOrganization)
            .options(selectinload(GSageUserOrganization.user))
            .where(GSageUserOrganization.org_id == org.id)
            .order_by(GSageUserOrganization.role)
        )
        memberships = result.scalars().all()

        print()
        print(_DIVIDER)
        print(f"  MEMBROS DA ORG: {org.name}  (slug: {org.slug})")
        print(_DIVIDER)
        if not memberships:
            print("  Nenhum membro encontrado.")
        else:
            fmt = "  {:<36}  {:<30}  {:<10}  {}"
            print(fmt.format("UUID", "Email", "Role", "Ativo"))
            print("  " + "-" * 90)
            for m in memberships:
                u = m.user
                print(fmt.format(str(u.id), u.email, m.role, "sim" if m.is_active else "não"))
        print(_DIVIDER)
        print()


async def _cmd_info(args: argparse.Namespace) -> None:
    async with _get_session_maker()() as session:
        result = await session.execute(
            select(GSageUser)
            .options(
                selectinload(GSageUser.memberships).selectinload(
                    GSageUserOrganization.organization
                ),
                selectinload(GSageUser.groups),
            )
            .where(GSageUser.email.ilike(args.email))
        )
        user = result.scalar_one_or_none()
        if not user:
            print(f"[ERRO] Usuário não encontrado: {args.email!r}")
            sys.exit(1)

        print()
        print(_DIVIDER)
        print(f"  USUÁRIO: {user.email}")
        print(_DIVIDER)
        print(f"  UUID:          {user.id}")
        print(f"  Nome:          {user.full_name}")
        print(f"  Ativo:         {'sim' if user.is_active else 'não'}")
        print(f"  Auth provider: {user.auth_provider}")
        print(f"  OTP ativo:     {'sim' if user.otp_enabled else 'não'}")
        if user.ai_instructions:
            instructions_preview = user.ai_instructions[:80].replace("\n", " ")
            print(f"  AI instructions: {instructions_preview}…")

        print()
        print("  Organizações:")
        if not user.memberships:
            print("    (nenhuma)")
        else:
            for m in user.memberships:
                org = m.organization
                active_label = "ativo" if m.is_active else "inativo"
                print(f"    [{m.role:7}]  {org.name}  (slug: {org.slug}, {active_label})")

        print()
        print("  Grupos:")
        if not user.groups:
            print("    (nenhum)")
        else:
            for g in user.groups:
                print(f"    {g.name}")

        print(_DIVIDER)
        print()


async def _cmd_reset_password(args: argparse.Namespace) -> None:
    if args.password:
        new_password = args.password
        auto_generated = False
    else:
        new_password = _make_password()
        auto_generated = True

    async with _get_session_maker()() as session:
        user = await _find_user(session, args.email)
        if not user:
            print(f"[ERRO] Usuário não encontrado: {args.email!r}")
            sys.exit(1)

        user.password_hash = hash_password(new_password)
        await session.commit()

    print()
    print(_DIVIDER)
    print(f"  SENHA REDEFINIDA: {args.email}")
    print(_DIVIDER)
    if auto_generated:
        print("  [SENHA GERADA AUTOMATICAMENTE — guarde em lugar seguro]")
    print(f"  Senha: {new_password}")
    print(_DIVIDER)
    print()


async def _cmd_update(args: argparse.Namespace) -> None:
    async with _get_session_maker()() as session:
        user = await _find_user(session, args.email)
        if not user:
            print(f"[ERRO] Usuário não encontrado: {args.email!r}")
            sys.exit(1)

        changed: list[str] = []

        if args.full_name is not None:
            user.full_name = args.full_name
            changed.append(f"full_name = {args.full_name!r}")

        if args.is_active is not None:
            user.is_active = args.is_active
            changed.append(f"is_active = {args.is_active}")

        if args.ai_instructions is not None:
            user.ai_instructions = args.ai_instructions or None
            changed.append("ai_instructions atualizado")

        if args.role is not None:
            if args.role not in _VALID_ROLES:
                print(f"[ERRO] Role inválido: {args.role!r}. Válidos: {', '.join(_VALID_ROLES)}")
                sys.exit(1)
            if not args.org:
                print("[ERRO] --org é obrigatório para atualizar o role.")
                sys.exit(1)
            org = await _resolve_org(session, args.org)
            if not org:
                print(f"[ERRO] Organização não encontrada: {args.org!r}")
                sys.exit(1)
            membership_result = await session.execute(
                select(GSageUserOrganization).where(
                    GSageUserOrganization.user_id == user.id,
                    GSageUserOrganization.org_id == org.id,
                )
            )
            membership = membership_result.scalar_one_or_none()
            if not membership:
                print(f"[ERRO] Usuário {args.email!r} não é membro de {args.org!r}.")
                sys.exit(1)
            membership.role = args.role
            changed.append(f"role = {args.role!r} (org: {org.name})")

        if not changed:
            print("[AVISO] Nenhuma alteração solicitada. Use --help para ver as opções.")
            return

        await session.commit()

    print()
    print(_DIVIDER)
    print(f"  USUÁRIO ATUALIZADO: {args.email}")
    print(_DIVIDER)
    for c in changed:
        print(f"  • {c}")
    print(_DIVIDER)
    print()


async def _cmd_add_to_group(args: argparse.Namespace) -> None:
    async with _get_session_maker()() as session:
        user = await _find_user(session, args.email)
        if not user:
            print(f"[ERRO] Usuário não encontrado: {args.email!r}")
            sys.exit(1)

        org = await _resolve_org(session, args.org)
        if not org:
            print(f"[ERRO] Organização não encontrada: {args.org!r}")
            sys.exit(1)

        # Verify membership
        membership_result = await session.execute(
            select(GSageUserOrganization).where(
                GSageUserOrganization.user_id == user.id,
                GSageUserOrganization.org_id == org.id,
            )
        )
        if not membership_result.scalar_one_or_none():
            print(f"[ERRO] Usuário {args.email!r} não é membro de {args.org!r}.")
            sys.exit(1)

        # Find group
        group_ref = args.group
        uid = _try_uuid(group_ref)
        if uid:
            group_result = await session.execute(
                select(GSageGroup).where(
                    GSageGroup.id == uid,
                    GSageGroup.org_id == org.id,
                )
            )
        else:
            group_result = await session.execute(
                select(GSageGroup).where(
                    GSageGroup.name.ilike(group_ref),
                    GSageGroup.org_id == org.id,
                )
            )
        group = group_result.scalar_one_or_none()
        if not group:
            print(f"[ERRO] Grupo {args.group!r} não encontrado na org {org.name!r}.")
            sys.exit(1)

        # Load user with groups to check membership
        user_with_groups = await session.execute(
            select(GSageUser)
            .options(selectinload(GSageUser.groups))
            .where(GSageUser.id == user.id)
        )
        user_loaded = user_with_groups.scalar_one()
        if any(g.id == group.id for g in user_loaded.groups):
            print(f"[AVISO] Usuário {args.email!r} já é membro do grupo {group.name!r}.")
            return

        user_loaded.groups.append(group)
        await session.commit()

    print()
    print(_DIVIDER)
    print(f"  USUÁRIO ADICIONADO AO GRUPO")
    print(_DIVIDER)
    print(f"  Usuário: {args.email}")
    print(f"  Grupo:   {group.name}")
    print(f"  Org:     {org.name}")
    print(_DIVIDER)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_users.py",
        description="Gerenciamento de usuários via linha de comando.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = subparsers.add_parser("create", help="Cria um usuário e adiciona a uma org.")
    p_create.add_argument("--email", required=True, help="Email do usuário")
    p_create.add_argument("--org", required=True, help="Org (UUID, slug ou nome)")
    p_create.add_argument("--full-name", dest="full_name", help="Nome completo")
    p_create.add_argument("--password", help="Senha (gerada automaticamente se omitido)")
    p_create.add_argument(
        "--role",
        default="member",
        choices=_VALID_ROLES,
        help="Role na org (default: member)",
    )

    # list
    p_list = subparsers.add_parser("list", help="Lista membros de uma org.")
    p_list.add_argument("--org", required=True, help="Org (UUID, slug ou nome)")

    # info
    p_info = subparsers.add_parser("info", help="Exibe detalhes de um usuário.")
    p_info.add_argument("--email", required=True, help="Email do usuário")

    # reset-password
    p_reset = subparsers.add_parser("reset-password", help="Redefine a senha de um usuário.")
    p_reset.add_argument("--email", required=True, help="Email do usuário")
    p_reset.add_argument("--password", help="Nova senha (gerada automaticamente se omitido)")

    # update
    p_update = subparsers.add_parser("update", help="Atualiza atributos de um usuário.")
    p_update.add_argument("--email", required=True, help="Email do usuário")
    p_update.add_argument("--full-name", dest="full_name", help="Novo nome completo")
    p_update.add_argument(
        "--active",
        dest="is_active",
        type=lambda x: x.lower() in ("1", "true", "yes", "sim"),
        metavar="true|false",
        help="Ativar ou desativar o usuário",
    )
    p_update.add_argument(
        "--ai-instructions",
        dest="ai_instructions",
        help="Instruções de AI personalizadas (string vazia para remover)",
    )
    p_update.add_argument(
        "--role",
        choices=_VALID_ROLES,
        help="Novo role do usuário (requer --org)",
    )
    p_update.add_argument("--org", help="Org (requerida quando --role é fornecido)")

    # add-to-group
    p_group = subparsers.add_parser("add-to-group", help="Adiciona um usuário a um grupo.")
    p_group.add_argument("--email", required=True, help="Email do usuário")
    p_group.add_argument("--org", required=True, help="Org (UUID, slug ou nome)")
    p_group.add_argument("--group", required=True, help="Nome ou UUID do grupo")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    cmd_map = {
        "create": _cmd_create,
        "list": _cmd_list,
        "info": _cmd_info,
        "reset-password": _cmd_reset_password,
        "update": _cmd_update,
        "add-to-group": _cmd_add_to_group,
    }

    fn = cmd_map[args.command]
    asyncio.run(fn(args))


if __name__ == "__main__":
    main()
