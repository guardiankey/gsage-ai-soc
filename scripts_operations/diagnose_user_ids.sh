#!/bin/bash
# Diagnóstico: verificar user_id atual vs user_ids no Weaviate

docker compose exec backend python -c "
import asyncio, sys
sys.path.insert(0, '/app')
from src.shared.weaviate_client import get_weaviate_client, COLLECTION_NAME

async def diagnose():
    client = await get_weaviate_client()
    try:
        col = client.collections.get(COLLECTION_NAME)
        
        # Agrupar por user_id
        objs = await col.query.fetch_objects(
            limit=100, 
            return_properties=['user_id', 'org_id', 'content']
        )
        
        user_ids = {}
        for o in objs.objects:
            uid = o.properties.get('user_id', '')
            org = o.properties.get('org_id', '')
            key = f'{org}:{uid}' if uid else f'{org}:ORG-LEVEL'
            user_ids[key] = user_ids.get(key, 0) + 1
        
        print('=== Distribuição de registros por org_id:user_id ===')
        for key, count in sorted(user_ids.items()):
            print(f'  {key}: {count} registros')
    
    finally:
        await client.close()

asyncio.run(diagnose())
"
