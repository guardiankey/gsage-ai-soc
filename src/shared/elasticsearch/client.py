"""gSage AI — Elasticsearch client utilities."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from elasticsearch import AsyncElasticsearch

from src.shared.config.settings import get_settings

logger = logging.getLogger(__name__)


class ElasticsearchClient:
    """Elasticsearch client wrapper with initialization and health check."""

    def __init__(self) -> None:
        """Initialize Elasticsearch client."""
        settings = get_settings()
        self.client = AsyncElasticsearch(
            hosts=[settings.elasticsearch_url],
            retry_on_timeout=True,
            max_retries=3,
        )
        self.index_prefix = settings.elasticsearch_index_prefix

    async def health_check(self) -> bool:
        """Check Elasticsearch cluster health.

        Returns:
            True if cluster is healthy, False otherwise.
        """
        try:
            health = await self.client.cluster.health()
            status = health.get("status", "red")
            return status in ("green", "yellow")
        except Exception as exc:
            logger.error("Elasticsearch health check failed: %s", exc)
            return False

    async def create_index_templates(
        self, templates: Dict[str, Dict[str, Any]]
    ) -> None:
        """Create index templates for all indices.

        Args:
            templates: Dictionary of template name -> template definition.
        """
        from src.shared.elasticsearch import INDEX_TEMPLATES

        for name, config in INDEX_TEMPLATES.items():
            try:
                # Delete old template without prefix if it exists (cleanup from previous runs)
                try:
                    await self.client.indices.delete_index_template(name=name)
                    logger.info(f"Deleted old template: {name}")
                except Exception:
                    pass  # Template doesn't exist, ignore
                
                # Create new template with prefix
                await self.client.indices.put_index_template(
                    name=f"{self.index_prefix}{name}",
                    index_patterns=config["index_patterns"],
                    template=config["template"],
                    priority=config.get("priority", 0),
                )
                logger.info(f"Created index template: {self.index_prefix}{name}")
            except Exception as exc:
                logger.error(f"Failed to create index template {name}: {exc}")

    async def create_ilm_policies(self, policies: Dict[str, Dict[str, Any]]) -> None:
        """Create Index Lifecycle Management policies.

        Args:
            policies: Dictionary of policy name -> policy definition.
        """
        from src.shared.elasticsearch import ILM_POLICIES

        for name, policy in ILM_POLICIES.items():
            try:
                await self.client.ilm.put_lifecycle(name=name, policy=policy)
                logger.info(f"Created ILM policy: {name}")
            except Exception as exc:
                logger.error(f"Failed to create ILM policy {name}: {exc}")

    async def close(self) -> None:
        """Close Elasticsearch client connection."""
        await self.client.close()


# Global client instance (lazy-loaded)
_es_client: Optional[ElasticsearchClient] = None


async def get_es_client() -> ElasticsearchClient:
    """Get or create global Elasticsearch client."""
    global _es_client
    if _es_client is None:
        _es_client = ElasticsearchClient()
    return _es_client
