"""gSage AI — Redis cache service."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Optional

import redis.asyncio as redis

from src.shared.config.settings import Settings
from src.shared.security.permissions import (
    api_key_cache_key,
    permission_cache_key,
    revoked_api_key_cache_key,
)

# TTL constants
PERMISSION_CACHE_TTL = timedelta(minutes=5)  # Per PROMPT.md
API_KEY_CACHE_TTL = timedelta(hours=1)
REVOKED_API_KEY_TTL = timedelta(days=90)  # Keep revocation for audit


class CacheService:
    """Redis cache service for permissions and API keys."""
    
    def __init__(self, settings: Settings):
        """
        Initialize cache service.
        
        Args:
            settings: Application settings with Redis configuration
        """
        self.settings = settings
        self._client: Optional[redis.Redis] = None
    
    async def connect(self) -> None:
        """Connect to Redis."""
        if self._client is None:
            self._client = redis.from_url(
                self.settings.redis_url,
                decode_responses=True,
            )
    
    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self._client is not None:
            await self._client.close()
            self._client = None
    
    @property
    def client(self) -> redis.Redis:
        """Get Redis client (must call connect() first)."""
        if self._client is None:
            raise RuntimeError("CacheService not connected. Call connect() first.")
        return self._client
    
    # Permission caching
    
    async def get_cached_permissions(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> Optional[tuple[list[uuid.UUID], list[str]]]:
        """
        Get cached user permissions.
        
        Args:
            user_id: User ID
            org_id: Organization ID
            
        Returns:
            Tuple of (group_ids, permission_tags) or None if not cached
        """
        key = permission_cache_key(user_id, org_id)
        data = await self.client.get(key)
        
        if data is None:
            return None
        
        cached = json.loads(data)
        group_ids = [uuid.UUID(gid) for gid in cached["group_ids"]]
        permission_tags = cached["permission_tags"]
        
        return group_ids, permission_tags
    
    async def set_cached_permissions(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        group_ids: list[uuid.UUID],
        permission_tags: list[str],
    ) -> None:
        """
        Cache user permissions.
        
        Args:
            user_id: User ID
            org_id: Organization ID
            group_ids: User's group IDs
            permission_tags: User's permission tags
        """
        key = permission_cache_key(user_id, org_id)
        data = {
            "group_ids": [str(gid) for gid in group_ids],
            "permission_tags": permission_tags,
        }
        
        await self.client.setex(
            key,
            PERMISSION_CACHE_TTL,
            json.dumps(data),
        )
    
    async def invalidate_user_permissions(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> None:
        """
        Invalidate cached user permissions.
        
        Args:
            user_id: User ID
            org_id: Organization ID
        """
        key = permission_cache_key(user_id, org_id)
        await self.client.delete(key)
    
    async def invalidate_group_permissions(
        self,
        group_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> None:
        """
        Invalidate permissions for all users in a group.
        
        When group permissions change, we need to invalidate all
        users in that group. This is a heavy operation.
        
        Args:
            group_id: Group ID
            org_id: Organization ID
        """
        # Pattern: cache:permissions:{org_id}:*
        pattern = f"cache:permissions:{org_id}:*"
        
        # Scan and delete matching keys
        cursor = 0
        while True:
            cursor, keys = await self.client.scan(cursor, match=pattern, count=100)
            if keys:
                await self.client.delete(*keys)
            if cursor == 0:
                break
    
    # API key caching
    
    async def get_cached_api_key(
        self,
        key_hash: str,
    ) -> Optional[dict[str, Any]]:
        """
        Get cached API key data.
        
        Args:
            key_hash: SHA-256 hash of the API key
            
        Returns:
            Dictionary with org_id, scoped_permissions, api_key_id or None
        """
        key = api_key_cache_key(key_hash)
        data = await self.client.get(key)
        
        if data is None:
            return None
        
        cached = json.loads(data)
        cached["org_id"] = uuid.UUID(cached["org_id"])
        cached["api_key_id"] = uuid.UUID(cached["api_key_id"])
        
        return cached
    
    async def set_cached_api_key(
        self,
        key_hash: str,
        org_id: uuid.UUID,
        scoped_permissions: list[str],
        api_key_id: uuid.UUID,
    ) -> None:
        """
        Cache API key data.
        
        Args:
            key_hash: SHA-256 hash of the API key
            org_id: Organization ID
            scoped_permissions: API key's allowed permissions
            api_key_id: API key ID
        """
        key = api_key_cache_key(key_hash)
        data = {
            "org_id": str(org_id),
            "scoped_permissions": scoped_permissions,
            "api_key_id": str(api_key_id),
        }
        
        await self.client.setex(
            key,
            API_KEY_CACHE_TTL,
            json.dumps(data),
        )
    
    async def invalidate_api_key(
        self,
        key_hash: str,
    ) -> None:
        """
        Invalidate cached API key.
        
        Args:
            key_hash: SHA-256 hash of the API key
        """
        key = api_key_cache_key(key_hash)
        await self.client.delete(key)
    
    # API key revocation
    
    async def mark_api_key_revoked(
        self,
        api_key_id: uuid.UUID,
    ) -> None:
        """
        Mark API key as revoked in cache.
        
        This provides instant revocation without waiting for DB sync.
        
        Args:
            api_key_id: API key ID
        """
        key = revoked_api_key_cache_key(api_key_id)
        await self.client.setex(
            key,
            REVOKED_API_KEY_TTL,
            "1",  # Just a marker
        )
    
    async def is_api_key_revoked(
        self,
        api_key_id: uuid.UUID,
    ) -> bool:
        """
        Check if API key is marked as revoked in cache.
        
        Args:
            api_key_id: API key ID
            
        Returns:
            True if revoked
        """
        key = revoked_api_key_cache_key(api_key_id)
        result = await self.client.exists(key)
        return result > 0
    
    # Rate limiting helpers
    
    async def increment_rate_limit(
        self,
        key: str,
        ttl_seconds: int = 60,
    ) -> int:
        """
        Increment rate limit counter.
        
        Args:
            key: Rate limit key (e.g., "ratelimit:apikey:{id}")
            ttl_seconds: TTL for counter (default: 60s)
            
        Returns:
            Current count
        """
        count = await self.client.incr(key)
        if count == 1:
            # First request, set TTL
            await self.client.expire(key, ttl_seconds)
        return count
    
    async def get_rate_limit_count(
        self,
        key: str,
    ) -> int:
        """
        Get current rate limit count.
        
        Args:
            key: Rate limit key
            
        Returns:
            Current count (0 if not set)
        """
        count = await self.client.get(key)
        return int(count) if count else 0


# Singleton instance
_cache_service: Optional[CacheService] = None


def get_cache_service(settings: Settings) -> CacheService:
    """
    Get singleton cache service instance.
    
    Args:
        settings: Application settings
        
    Returns:
        CacheService instance
    """
    global _cache_service
    if _cache_service is None:
        _cache_service = CacheService(settings)
    return _cache_service
