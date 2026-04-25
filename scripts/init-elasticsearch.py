#!/usr/bin/env python3
"""Initialize Elasticsearch indices and ILM policies for gSage AI."""

import asyncio
import sys
import logging
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


async def main() -> int:
    """Initialize Elasticsearch indices and policies."""
    es_client = None
    try:
        from src.shared.elasticsearch import ILM_POLICIES, INDEX_TEMPLATES
        from src.shared.elasticsearch.client import get_es_client

        logger.info("Connecting to Elasticsearch...")
        es_client = await get_es_client()

        # Health check
        logger.info("Checking Elasticsearch health...")
        healthy = await es_client.health_check()
        if not healthy:
            logger.error("Elasticsearch cluster is not healthy!")
            return 1

        logger.info("✓ Elasticsearch cluster is healthy")

        # Create ILM policies
        logger.info("Creating ILM policies...")
        await es_client.create_ilm_policies(ILM_POLICIES)
        logger.info("✓ ILM policies created")

        # Create index templates
        logger.info("Creating index templates...")
        await es_client.create_index_templates(INDEX_TEMPLATES)
        logger.info("✓ Index templates created")

        logger.info("✓ Elasticsearch initialization complete!")
        return 0

    except Exception as exc:
        logger.error(f"Failed to initialize Elasticsearch: {exc}", exc_info=True)
        return 1
    finally:
        if es_client:
            await es_client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
