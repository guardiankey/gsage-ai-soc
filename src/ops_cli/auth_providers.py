"""ops_cli — auth provider configuration (per-org auth chain + SSO settings).

Usage (inside backend_api container)::

    # Set the ordered auth provider chain for an org
    python -m ops_cli auth-providers set \\
        --org-slug acme \\
        --providers entra_oidc,local

    # Configure the Entra OIDC provider for an org
    python -m ops_cli auth-providers config \\
        --org-slug acme \\
        --provider entra_oidc \\
        --client-id 11111111-1111-1111-1111-111111111111 \\
        --tenant-id 22222222-2222-2222-2222-222222222222 \\
        --client-secret-stdin \\
        --default-role viewer

    # Manage email-domain → org mappings (used by /v1/auth/lookup)
    python -m ops_cli auth-providers domain add --org-slug acme --domain acme.com
    python -m ops_cli auth-providers domain list --org-slug acme
    python -m ops_cli auth-providers domain remove --domain acme.com

    # Show the current chain + provider config (sensitive fields redacted)
    python -m ops_cli auth-providers show --org-slug acme

    # Round-trip editing of the group_mapping JSON
    python -m ops_cli auth-providers get-mapping \\
        --org-slug acme --provider entra_oidc > /tmp/mapping.json
    # ... edit /tmp/mapping.json ...
    python -m ops_cli auth-providers config \\
        --org-slug acme --provider entra_oidc \\
        --auto-create-departments true \\
        --group-mapping-stdin < /tmp/mapping.json

    # Round-trip editing of the FULL provider config (decrypted, includes
    # client_secret in clear text — handle the file with care!).
    python -m ops_cli auth-providers get-config \\
        --org-slug acme --provider entra_oidc > /tmp/cfg.json
    # ... edit /tmp/cfg.json ...
    python -m ops_cli auth-providers config \\
        --org-slug acme --provider entra_oidc \\
        --config-stdin < /tmp/cfg.json
    shred -u /tmp/cfg.json   # or `rm -P` on macOS

group_mapping JSON shape (per provider)::

    {
      "<entra_group_object_id>": {
        "role": "member",                       // org-wide role
        "groups": ["soc-analysts"],             // local GSageGroup names
        "department": "Security Ops",           // single department (legacy)
        "departments": ["Security Ops","NOC"],  // multi-department
        "dept_role": "member"                   // role inside the dept(s);
                                                // may also be a list aligned
                                                // with `departments`. If
                                                // shorter than the dept list,
                                                // the last value is repeated.
      }
    }
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Optional

from sqlalchemy import select

from src.ops_cli._helpers import print_result, resolve_org_id


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)

    # set-chain
    s = sub.add_parser("set", help="Set the ordered auth provider chain for an org")
    s.add_argument("--org-id", default=None)
    s.add_argument("--org-slug", default=None)
    s.add_argument("--providers", required=True,
                   help="Comma-separated provider names, e.g. 'entra_oidc,local'")
    s.add_argument("--json", dest="json_out", action="store_true")
    s.set_defaults(_func=_run_set)

    # config
    c = sub.add_parser("config", help="Configure a specific provider for an org")
    c.add_argument("--org-id", default=None)
    c.add_argument("--org-slug", default=None)
    c.add_argument("--provider", required=True,
                   help="Provider name (e.g. 'entra_oidc')")
    c.add_argument("--client-id", default=None)
    c.add_argument("--tenant-id", default=None)
    c.add_argument("--client-secret", default=None,
                   help="Prefer --client-secret-stdin (does not appear in shell history)")
    c.add_argument("--client-secret-stdin", action="store_true")
    c.add_argument("--redirect-uri", default=None)
    c.add_argument("--scopes", default=None)
    c.add_argument("--default-role", default=None)
    c.add_argument("--auto-provision-users", choices=["true", "false"], default=None)
    c.add_argument("--auto-create-groups", choices=["true", "false"], default=None)
    c.add_argument("--auto-create-departments", choices=["true", "false"], default=None,
                   help="When true, missing departments referenced in group_mapping are auto-created")
    c.add_argument("--required-groups", default=None,
                   help="Comma-separated list of group object IDs (login gate)")
    c.add_argument("--group-mapping-json", default=None,
                   help="JSON string for group_mapping (overrides existing)")
    c.add_argument("--group-mapping-stdin", action="store_true",
                   help="Read group_mapping JSON from stdin (overrides existing). "
                        "Useful for editing larger mappings via a file: "
                        "`get-mapping ... > m.json && edit && config ... --group-mapping-stdin < m.json`")
    c.add_argument("--config-stdin", action="store_true",
                   help="Read the FULL provider config JSON from stdin and replace "
                        "the existing block. Pairs with `get-config` for round-trip "
                        "editing. Other --client-id/--tenant-id/... flags are still "
                        "applied on top of the stdin payload.")
    c.add_argument("--clear", action="store_true",
                   help="Remove the provider's config block entirely")
    c.add_argument("--json", dest="json_out", action="store_true")
    c.set_defaults(_func=_run_config)

    # show
    sh = sub.add_parser("show", help="Show current auth chain and provider config (redacted)")
    sh.add_argument("--org-id", default=None)
    sh.add_argument("--org-slug", default=None)
    sh.add_argument("--json", dest="json_out", action="store_true")
    sh.set_defaults(_func=_run_show)

    # get-mapping (raw JSON dump of group_mapping for editing + resubmission)
    gm = sub.add_parser(
        "get-mapping",
        help="Print the raw group_mapping JSON for a provider (for round-trip editing)",
    )
    gm.add_argument("--org-id", default=None)
    gm.add_argument("--org-slug", default=None)
    gm.add_argument("--provider", required=True)
    gm.set_defaults(_func=_run_get_mapping)

    # get-config (raw JSON dump of full provider config — NOT redacted)
    gc = sub.add_parser(
        "get-config",
        help="Print the raw decrypted provider config JSON (includes secrets!)",
    )
    gc.add_argument("--org-id", default=None)
    gc.add_argument("--org-slug", default=None)
    gc.add_argument("--provider", required=True)
    gc.set_defaults(_func=_run_get_config)

    # get-templates (raw JSON dump of org-level permission_templates)
    gt = sub.add_parser(
        "get-templates",
        help="Print the org-level permission_templates JSON (for round-trip editing)",
    )
    gt.add_argument("--org-id", default=None)
    gt.add_argument("--org-slug", default=None)
    gt.set_defaults(_func=_run_get_templates)

    # set-templates (replace the entire permission_templates dict)
    st = sub.add_parser(
        "set-templates",
        help=(
            "Replace org-level permission_templates from JSON read on stdin. "
            "Pass an empty object '{}' to clear."
        ),
    )
    st.add_argument("--org-id", default=None)
    st.add_argument("--org-slug", default=None)
    st.add_argument(
        "--stdin",
        dest="from_stdin",
        action="store_true",
        required=True,
        help="Read the templates JSON from stdin (required for safety).",
    )
    st.add_argument("--json", dest="json_out", action="store_true")
    st.set_defaults(_func=_run_set_templates)

    # domain add/list/remove
    dom = sub.add_parser("domain", help="Manage email-domain → org mappings")
    dom_sub = dom.add_subparsers(dest="domain_action", required=True)

    dadd = dom_sub.add_parser("add", help="Map an email domain to an org")
    dadd.add_argument("--org-id", default=None)
    dadd.add_argument("--org-slug", default=None)
    dadd.add_argument("--domain", required=True)
    dadd.add_argument("--json", dest="json_out", action="store_true")
    dadd.set_defaults(_func=_run_domain_add)

    dlist = dom_sub.add_parser("list", help="List domains mapped to an org")
    dlist.add_argument("--org-id", default=None)
    dlist.add_argument("--org-slug", default=None)
    dlist.add_argument("--json", dest="json_out", action="store_true")
    dlist.set_defaults(_func=_run_domain_list)

    drm = dom_sub.add_parser("remove", help="Remove a domain → org mapping")
    drm.add_argument("--domain", required=True)
    drm.add_argument("--json", dest="json_out", action="store_true")
    drm.set_defaults(_func=_run_domain_remove)


# ---------------------------------------------------------------------------
# Sync wrappers
# ---------------------------------------------------------------------------


def _run_set(args: argparse.Namespace) -> int:
    return asyncio.run(_set_chain_async(args))


def _run_config(args: argparse.Namespace) -> int:
    return asyncio.run(_config_async(args))


def _run_show(args: argparse.Namespace) -> int:
    return asyncio.run(_show_async(args))


def _run_get_mapping(args: argparse.Namespace) -> int:
    return asyncio.run(_get_mapping_async(args))


def _run_get_config(args: argparse.Namespace) -> int:
    return asyncio.run(_get_config_async(args))


def _run_get_templates(args: argparse.Namespace) -> int:
    return asyncio.run(_get_templates_async(args))


def _run_set_templates(args: argparse.Namespace) -> int:
    return asyncio.run(_set_templates_async(args))


def _run_domain_add(args: argparse.Namespace) -> int:
    return asyncio.run(_domain_add_async(args))


def _run_domain_list(args: argparse.Namespace) -> int:
    return asyncio.run(_domain_list_async(args))


def _run_domain_remove(args: argparse.Namespace) -> int:
    return asyncio.run(_domain_remove_async(args))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_KNOWN_PROVIDERS = {"local", "ldap", "entra_oidc"}


def _read_secret(args: argparse.Namespace) -> Optional[str]:
    if getattr(args, "client_secret_stdin", False):
        raw = sys.stdin.read()
        for line in raw.splitlines():
            line = line.strip()
            if line:
                return line
        return None
    return args.client_secret


def _bool_or_none(v: Optional[str]) -> Optional[bool]:
    if v is None:
        return None
    return v.lower() == "true"


def _redact(provider: str, cfg: dict) -> dict:
    """Return a copy of *cfg* with sensitive fields redacted."""
    sensitive = {"client_secret", "bind_password", "_password_hash"}
    return {k: ("***" if k in sensitive and v else v) for k, v in cfg.items()}


# ---------------------------------------------------------------------------
# Implementation: set-chain
# ---------------------------------------------------------------------------


async def _set_chain_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models import GSageOrganization  # noqa: PLC0415

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    unknown = [p for p in providers if p not in _KNOWN_PROVIDERS]
    if unknown:
        print(
            f"ERROR: unknown provider(s): {', '.join(unknown)}. "
            f"Known providers: {', '.join(sorted(_KNOWN_PROVIDERS))}",
            file=sys.stderr,
        )
        return 2

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)
        org = await db.get(GSageOrganization, org_id)
        if org is None:
            print(f"ERROR: org {org_id} not found", file=sys.stderr)
            return 1
        org.auth_providers = providers
        await db.commit()

    print_result(
        {
            "status": "ok",
            "message": "auth provider chain updated",
            "details": {"providers": providers},
        },
        json_out=args.json_out,
    )
    return 0


# ---------------------------------------------------------------------------
# Implementation: config
# ---------------------------------------------------------------------------


async def _config_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models import GSageOrganization  # noqa: PLC0415

    if args.provider not in _KNOWN_PROVIDERS:
        print(
            f"ERROR: unknown provider '{args.provider}'. "
            f"Known: {', '.join(sorted(_KNOWN_PROVIDERS))}",
            file=sys.stderr,
        )
        return 2

    # Only one stdin consumer at a time
    stdin_flags = [
        ("--client-secret-stdin", getattr(args, "client_secret_stdin", False)),
        ("--group-mapping-stdin", getattr(args, "group_mapping_stdin", False)),
        ("--config-stdin", getattr(args, "config_stdin", False)),
    ]
    active_stdin = [name for name, on in stdin_flags if on]
    if len(active_stdin) > 1:
        print(
            f"ERROR: only one stdin source allowed per call, got: "
            f"{', '.join(active_stdin)}",
            file=sys.stderr,
        )
        return 2

    # Read full-config payload up-front (used as the base before per-flag
    # overrides). Keeps stdin a single-shot read.
    config_payload: Optional[dict[str, Any]] = None
    if args.config_stdin:
        raw = sys.stdin.read()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"ERROR: --config-stdin is not valid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(parsed, dict):
            print("ERROR: --config-stdin payload must be a JSON object", file=sys.stderr)
            return 2
        config_payload = parsed

    secret = _read_secret(args)

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)
        org = await db.get(GSageOrganization, org_id)
        if org is None:
            print(f"ERROR: org {org_id} not found", file=sys.stderr)
            return 1

        all_config: dict[str, Any] = dict(org.auth_config or {})
        if args.clear:
            all_config.pop(args.provider, None)
            org.auth_config = all_config
            await db.commit()
            print_result(
                {"status": "ok", "message": f"cleared config for '{args.provider}'"},
                json_out=args.json_out,
            )
            return 0

        cfg: dict[str, Any] = dict(all_config.get(args.provider) or {})

        # Full-config replacement first; per-flag overrides apply on top.
        if config_payload is not None:
            cfg = dict(config_payload)

        if args.client_id is not None:
            cfg["client_id"] = args.client_id
        if args.tenant_id is not None:
            cfg["tenant_id"] = args.tenant_id
        if secret is not None:
            cfg["client_secret"] = secret
        if args.redirect_uri is not None:
            cfg["redirect_uri"] = args.redirect_uri
        if args.scopes is not None:
            cfg["scopes"] = args.scopes
        if args.default_role is not None:
            cfg["default_role"] = args.default_role

        auto_prov = _bool_or_none(args.auto_provision_users)
        if auto_prov is not None:
            cfg["auto_provision_users"] = auto_prov

        auto_grp = _bool_or_none(args.auto_create_groups)
        if auto_grp is not None:
            cfg["auto_create_groups"] = auto_grp

        auto_dept = _bool_or_none(args.auto_create_departments)
        if auto_dept is not None:
            cfg["auto_create_departments"] = auto_dept

        if args.required_groups is not None:
            cfg["required_groups"] = [
                g.strip() for g in args.required_groups.split(",") if g.strip()
            ]
        if args.group_mapping_stdin:
            raw = sys.stdin.read()
            try:
                cfg["group_mapping"] = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"ERROR: stdin is not valid JSON: {exc}", file=sys.stderr)
                return 2
        elif args.group_mapping_json is not None:
            try:
                cfg["group_mapping"] = json.loads(args.group_mapping_json)
            except json.JSONDecodeError as exc:
                print(f"ERROR: --group-mapping-json is not valid JSON: {exc}", file=sys.stderr)
                return 2

        all_config[args.provider] = cfg
        org.auth_config = all_config
        await db.commit()

    print_result(
        {
            "status": "ok",
            "message": f"config updated for '{args.provider}'",
            "details": _redact(args.provider, cfg),
        },
        json_out=args.json_out,
    )
    return 0


# ---------------------------------------------------------------------------
# Implementation: show
# ---------------------------------------------------------------------------


async def _show_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models import GSageOrganization, GSageOrgEmailDomain  # noqa: PLC0415

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)
        org = await db.get(GSageOrganization, org_id)
        if org is None:
            print(f"ERROR: org {org_id} not found", file=sys.stderr)
            return 1

        all_config = org.auth_config or {}
        redacted_config = {p: _redact(p, c or {}) for p, c in all_config.items()}

        domains_res = await db.execute(
            select(GSageOrgEmailDomain.domain).where(
                GSageOrgEmailDomain.org_id == org.id
            ).order_by(GSageOrgEmailDomain.domain)
        )
        domains = [row[0] for row in domains_res.all()]

    payload = {
        "status": "ok",
        "message": f"auth config for org '{org.slug}'",
        "details": {
            "providers": list(org.auth_providers or []),
            "config": redacted_config,
            "email_domains": domains,
        },
    }
    print_result(payload, json_out=args.json_out)
    return 0


# ---------------------------------------------------------------------------
# Implementation: get-mapping (raw JSON dump for round-trip editing)
# ---------------------------------------------------------------------------


async def _get_mapping_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models import GSageOrganization  # noqa: PLC0415

    if args.provider not in _KNOWN_PROVIDERS:
        print(
            f"ERROR: unknown provider '{args.provider}'. "
            f"Known: {', '.join(sorted(_KNOWN_PROVIDERS))}",
            file=sys.stderr,
        )
        return 2

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)
        org = await db.get(GSageOrganization, org_id)
        if org is None:
            print(f"ERROR: org {org_id} not found", file=sys.stderr)
            return 1
        cfg = (org.auth_config or {}).get(args.provider) or {}
        mapping = cfg.get("group_mapping") or {}

    # Print bare JSON to stdout — designed for piping into a file and
    # later resubmission via `config ... --group-mapping-stdin < file.json`.
    json.dump(mapping, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------------------
# Implementation: get-config (raw decrypted full provider config — secrets!)
# ---------------------------------------------------------------------------


async def _get_config_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models import GSageOrganization  # noqa: PLC0415

    if args.provider not in _KNOWN_PROVIDERS:
        print(
            f"ERROR: unknown provider '{args.provider}'. "
            f"Known: {', '.join(sorted(_KNOWN_PROVIDERS))}",
            file=sys.stderr,
        )
        return 2

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)
        org = await db.get(GSageOrganization, org_id)
        if org is None:
            print(f"ERROR: org {org_id} not found", file=sys.stderr)
            return 1
        cfg = (org.auth_config or {}).get(args.provider) or {}

    # Bare JSON to stdout. NOT redacted: includes client_secret / bind_password
    # in clear text so the round-trip via `config --config-stdin` preserves
    # them. Treat the resulting file as a secret.
    json.dump(cfg, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------------------
# Implementation: get-templates / set-templates (org-level permission templates)
# ---------------------------------------------------------------------------


async def _get_templates_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models import GSageOrganization  # noqa: PLC0415

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)
        org = await db.get(GSageOrganization, org_id)
        if org is None:
            print(f"ERROR: org {org_id} not found", file=sys.stderr)
            return 1
        templates = (org.auth_config or {}).get("permission_templates") or {}

    json.dump(templates, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _validate_templates_payload(payload: object) -> tuple[Optional[dict], Optional[str]]:
    """Validate the shape of a templates payload.

    Returns ``(cleaned, error)``. On error ``cleaned`` is None.
    """
    if not isinstance(payload, dict):
        return None, "templates payload must be a JSON object"

    cleaned: dict = {}
    for name, body in payload.items():
        if not isinstance(name, str) or not name.strip():
            return None, f"invalid template name: {name!r}"
        if ":" in name:
            return None, (
                f"template name '{name}' must not contain ':' "
                "(reserved for managed-group naming)"
            )
        if not isinstance(body, dict):
            return None, f"template '{name}': value must be an object"
        perms = body.get("permissions")
        if not isinstance(perms, list) or not all(
            isinstance(p, str) and p.strip() for p in perms
        ):
            return None, (
                f"template '{name}': 'permissions' must be a list of permission tags"
            )
        description = body.get("description")
        if description is not None and not isinstance(description, str):
            return None, f"template '{name}': 'description' must be a string"
        entry: dict = {"permissions": list(perms)}
        if description:
            entry["description"] = description
        cleaned[name.strip()] = entry
    return cleaned, None


async def _set_templates_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models import GSageOrganization  # noqa: PLC0415

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON on stdin: {exc}", file=sys.stderr)
        return 2

    cleaned, err = _validate_templates_payload(payload)
    if err is not None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    assert cleaned is not None

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)
        org = await db.get(GSageOrganization, org_id)
        if org is None:
            print(f"ERROR: org {org_id} not found", file=sys.stderr)
            return 1
        all_config: dict[str, Any] = dict(org.auth_config or {})
        all_config["permission_templates"] = cleaned
        org.auth_config = all_config
        await db.commit()

    print_result(
        {
            "status": "ok",
            "message": (
                f"set {len(cleaned)} permission template(s) on org '{org.slug}'"
            ),
            "details": {"templates": sorted(cleaned.keys())},
        },
        json_out=args.json_out,
    )
    return 0


# ---------------------------------------------------------------------------
# Implementation: domain add/list/remove
# ---------------------------------------------------------------------------


async def _domain_add_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models import GSageOrgEmailDomain  # noqa: PLC0415

    domain = args.domain.strip().lower()
    if not domain or "." not in domain:
        print(f"ERROR: invalid domain {args.domain!r}", file=sys.stderr)
        return 2

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)

        existing = await db.execute(
            select(GSageOrgEmailDomain).where(GSageOrgEmailDomain.domain == domain)
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            if row.org_id == org_id:
                print_result(
                    {
                        "status": "ok",
                        "message": "domain already mapped to this org (no change)",
                        "details": {"domain": domain},
                    },
                    json_out=args.json_out,
                )
                return 0
            print(
                f"ERROR: domain {domain!r} is already mapped to a different org",
                file=sys.stderr,
            )
            return 1

        db.add(GSageOrgEmailDomain(org_id=org_id, domain=domain))
        await db.commit()

    print_result(
        {
            "status": "ok",
            "message": "domain mapped",
            "details": {"domain": domain},
        },
        json_out=args.json_out,
    )
    return 0


async def _domain_list_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models import GSageOrgEmailDomain  # noqa: PLC0415

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)
        res = await db.execute(
            select(GSageOrgEmailDomain.domain).where(
                GSageOrgEmailDomain.org_id == org_id
            ).order_by(GSageOrgEmailDomain.domain)
        )
        domains = [row[0] for row in res.all()]

    print_result(
        {
            "status": "ok",
            "message": f"{len(domains)} domain(s) mapped",
            "details": {"domains": domains},
        },
        json_out=args.json_out,
    )
    return 0


async def _domain_remove_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models import GSageOrgEmailDomain  # noqa: PLC0415

    domain = args.domain.strip().lower()
    session_maker = _get_session_maker()
    async with session_maker() as db:
        res = await db.execute(
            select(GSageOrgEmailDomain).where(GSageOrgEmailDomain.domain == domain)
        )
        row = res.scalar_one_or_none()
        if row is None:
            print(f"ERROR: domain {domain!r} not found", file=sys.stderr)
            return 1
        await db.delete(row)
        await db.commit()

    print_result(
        {
            "status": "ok",
            "message": "domain unmapped",
            "details": {"domain": domain},
        },
        json_out=args.json_out,
    )
    return 0
