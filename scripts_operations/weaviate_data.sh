#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# weaviate_data.sh — Inspect all Weaviate collections used by gSageKey
#
# Collections:
#   KnowledgeBase       — shared collection (weaviate_client.py), tenant-filtered by org_id/user_id
#   kb_{org_id}         — per-org collections created by agno Knowledge (knowledge.py)
#
# Usage:
#   ./scripts_operations/weaviate_data.sh          # summary + first 5 objects per collection
#   ./scripts_operations/weaviate_data.sh 20       # first 20 objects per collection
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

LIMIT="${1:-5}"

docker compose exec backend_api python -c "
import asyncio, sys, json
from collections import defaultdict

sys.path.insert(0, '/app')
from src.shared.weaviate_client import get_weaviate_client, COLLECTION_NAME

LIMIT = int('${LIMIT}')

# All properties from the KnowledgeBase schema
KB_PROPS = [
    'content', 'org_id', 'user_id', 'source',
    'is_validated', 'version', 'is_active',
    'tags', 'superseded_by_id', 'created_at', 'expires_at',
]


def fmt_val(v):
    if v is None:
        return '-'
    if isinstance(v, list):
        return ', '.join(str(x) for x in v) if v else '[]'
    if isinstance(v, str) and len(v) > 120:
        return v[:120] + '...'
    return str(v)


async def inspect_collection(client, name, props, limit):
    col = client.collections.get(name)
    agg = await col.aggregate.over_all(total_count=True)
    total = agg.total_count
    print(f'\n{'=' * 70}')
    print(f'  Collection: {name}   |   Total objects: {total}')
    print('=' * 70)

    if total == 0:
        print('  (empty)')
        return

    # ── Aggregate by org_id / user_id if properties exist ─────────────
    if 'org_id' in props:
        objs_all = await col.query.fetch_objects(
            limit=min(total, 500),
            return_properties=['org_id', 'user_id'],
        )
        org_counts = defaultdict(int)
        user_counts = defaultdict(int)
        for o in objs_all.objects:
            org = o.properties.get('org_id', '?')
            usr = o.properties.get('user_id', '?')
            org_counts[org] += 1
            user_counts[f'{org} / {usr}'] += 1

        print('\n  ── Objects per Organization ──')
        for org, cnt in sorted(org_counts.items()):
            print(f'     org={org}  count={cnt}')

        print('\n  ── Objects per Org/User ──')
        for key, cnt in sorted(user_counts.items()):
            print(f'     {key}  count={cnt}')

    # ── Detailed listing ──────────────────────────────────────────────
    objs = await col.query.fetch_objects(
        limit=limit,
        return_properties=props,
        include_vector=False,
    )
    print(f'\n  ── First {min(limit, total)} object(s) ──')
    for i, o in enumerate(objs.objects, 1):
        print(f'\n  [{i}] uuid={o.uuid}')
        for p in props:
            val = o.properties.get(p)
            if val is not None:
                print(f'      {p:20s} = {fmt_val(val)}')


async def main():
    client = await get_weaviate_client()
    try:
        # ── List all collections ──────────────────────────────────────
        all_cols = await client.collections.list_all()
        col_names = sorted(all_cols.keys()) if hasattr(all_cols, 'keys') else sorted(str(c) for c in all_cols)
        print(f'Weaviate collections found: {len(col_names)}')
        for n in col_names:
            print(f'  - {n}')

        # ── Inspect KnowledgeBase (shared collection) ─────────────────
        if COLLECTION_NAME in col_names:
            await inspect_collection(client, COLLECTION_NAME, KB_PROPS, LIMIT)
        else:
            print(f'\n⚠  Collection \"{COLLECTION_NAME}\" not found.')

        # ── Inspect per-org kb_* collections (agno Knowledge) ─────────
        kb_cols = [n for n in col_names if n.lower().startswith('kb_')]
        if kb_cols:
            for name in kb_cols:
                # agno collections have fewer properties; fetch what's available
                col = client.collections.get(name)
                agg = await col.aggregate.over_all(total_count=True)
                total = agg.total_count
                print(f'\n{'=' * 70}')
                print(f'  Collection: {name} (agno per-org)   |   Total objects: {total}')
                print('=' * 70)
                if total == 0:
                    print('  (empty)')
                    continue
                objs = await col.query.fetch_objects(limit=min(LIMIT, total), include_vector=False)
                for i, o in enumerate(objs.objects, 1):
                    print(f'\n  [{i}] uuid={o.uuid}')
                    for k, v in o.properties.items():
                        print(f'      {k:20s} = {fmt_val(v)}')
        else:
            print('\n  No per-org kb_* collections found (agno Knowledge).')

    finally:
        await client.close()

asyncio.run(main())
"
