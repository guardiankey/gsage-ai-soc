#!/usr/bin/env python3
"""Test Elasticsearch connection and initialize indices."""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

async def main():
    from src.shared.config.settings import get_settings
    from elasticsearch import AsyncElasticsearch
    
    settings = get_settings()
    print(f"Connecting to Elasticsearch at {settings.elasticsearch_url}...")
    
    es = AsyncElasticsearch(
        hosts=[settings.elasticsearch_url],
        retry_on_timeout=True,
        max_retries=3,
    )
    
    try:
        # Test connection
        info = await es.info()
        print(f"✓ Connected to Elasticsearch {info['version']['number']}")
        
        # Check health
        health = await es.cluster.health()
        print(f"✓ Cluster status: {health['status']}")
        
        # List existing indices
        indices = await es.cat.indices(format='json')
        print(f"✓ Existing indices: {len(indices)}")
        
        # Now initialize templates
        from src.shared.elasticsearch import INDEX_TEMPLATES, ILM_POLICIES
        
        print(f"\nCreating {len(ILM_POLICIES)} ILM policies...")
        for name, policy in ILM_POLICIES.items():
            try:
                await es.ilm.put_lifecycle(name=name, policy=policy)
                print(f"  ✓ {name}")
            except Exception as e:
                print(f"  ✗ {name}: {e}")
        
        print(f"\nCreating {len(INDEX_TEMPLATES)} index templates...")
        for name, template in INDEX_TEMPLATES.items():
            try:
                await es.indices.put_index_template(name=name, **template)
                print(f"  ✓ {name}")
            except Exception as e:
                print(f"  ✗ {name}: {e}")
        
        print("\n✓ Elasticsearch initialization complete!")
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        await es.close()
    
    return 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
