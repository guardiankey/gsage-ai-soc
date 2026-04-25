"""gSage AI — API key management service."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config.settings import Settings
from src.shared.models import GSageAPIKey
from src.shared.security.auth import (
    calculate_api_key_expiration,
    generate_api_key,
)
from src.shared.security.permissions import (
    resolve_api_key_permissions,
    validate_permission_tags,
)
from src.shared.services.cache_service import CacheService, get_cache_service


class APIKeyService:
    """Service for managing API keys."""
    
    def __init__(
        self,
        session: AsyncSession,
        cache_service: CacheService,
    ):
        """
        Initialize API key service.
        
        Args:
            session: Database session
            cache_service: Cache service for Redis operations
        """
        self.session = session
        self.cache = cache_service
    
    async def create_api_key(
        self,
        org_id: uuid.UUID,
        name: str,
        scoped_permissions: list[str],
        days_until_expiration: int,
        rate_limit_per_minute: int = 10,
        environment: str = "live",
        user_id: Optional[uuid.UUID] = None,
    ) -> tuple[str, GSageAPIKey]:
        """
        Create a new API key.
        
        Args:
            org_id: Organization ID
            name: Human-readable key name
            scoped_permissions: List of permission tags this key allows
            days_until_expiration: Days until key expires (max 365)
            rate_limit_per_minute: Optional rate limit (requests per minute)
            
        Returns:
            Tuple of (raw_key, api_key_model)
            
        Raises:
            ValueError: If scoped permissions are invalid or expiration > 365 days
            
        Note:
            The raw key is only returned once! Store it securely.
        """
        # Validate expiration
        if days_until_expiration < 1 or days_until_expiration > 365:
            raise ValueError("Expiration must be between 1 and 365 days")
        
        # Validate permission tags
        await validate_permission_tags(self.session, scoped_permissions)
        
        # Generate key
        raw_key, key_hash, key_prefix = generate_api_key(environment)
        expires_at = calculate_api_key_expiration(days_until_expiration)
        
        # Create model
        api_key = GSageAPIKey(
            org_id=org_id,
            user_id=user_id,
            name=name,
            key_hash=key_hash,
            key_prefix=key_prefix,
            environment=environment,
            scoped_permissions=scoped_permissions,
            expires_at=expires_at,
            rate_limit_per_minute=max(1, rate_limit_per_minute),  # min 1 req/min
            is_active=True,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        
        self.session.add(api_key)
        await self.session.commit()
        await self.session.refresh(api_key)
        
        # Cache the key
        await self.cache.set_cached_api_key(
            key_hash=key_hash,
            org_id=org_id,
            scoped_permissions=scoped_permissions,
            api_key_id=api_key.id,
        )
        
        return raw_key, api_key
    
    async def validate_api_key(
        self,
        raw_key: str,
    ) -> tuple[uuid.UUID, list[str], uuid.UUID]:
        """
        Validate API key and return its scope.
        
        Args:
            raw_key: Raw API key (gk_...)
            
        Returns:
            Tuple of (org_id, scoped_permissions, api_key_id)
            
        Raises:
            ValueError: If key is invalid, expired, or revoked
        """
        from src.shared.security.auth import hash_api_key as _hash_api_key
        key_hash = _hash_api_key(raw_key)
        
        # Check cache first
        cached = await self.cache.get_cached_api_key(key_hash)
        if cached:
            # Verify not revoked
            if await self.cache.is_api_key_revoked(cached["api_key_id"]):
                raise ValueError("API key has been revoked")
            
            return (
                cached["org_id"],
                cached["scoped_permissions"],
                cached["api_key_id"],
            )
        
        # Cache miss - query database
        org_id, scoped_permissions, api_key_id = await resolve_api_key_permissions(
            self.session,
            key_hash,
        )
        
        # Check revocation
        if await self.cache.is_api_key_revoked(api_key_id):
            raise ValueError("API key has been revoked")
        
        # Cache for next time
        await self.cache.set_cached_api_key(
            key_hash=key_hash,
            org_id=org_id,
            scoped_permissions=scoped_permissions,
            api_key_id=api_key_id,
        )
        
        return org_id, scoped_permissions, api_key_id
    
    async def revoke_api_key(
        self,
        api_key_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> None:
        """
        Revoke an API key (instant via Redis).
        
        Args:
            api_key_id: API key ID
            org_id: Organization ID (for validation)
            
        Raises:
            ValueError: If key not found or already revoked
        """
        stmt = select(GSageAPIKey).where(
            GSageAPIKey.id == api_key_id,
            GSageAPIKey.org_id == org_id,
        )
        result = await self.session.execute(stmt)
        api_key = result.scalar_one_or_none()
        
        if not api_key:
            raise ValueError("API key not found")
        
        if api_key.revoked_at is not None:
            raise ValueError("API key already revoked")
        
        # Mark as revoked in DB
        api_key.revoked_at = datetime.now(timezone.utc)
        api_key.is_active = False
        await self.session.commit()
        
        # Mark as revoked in Redis (instant effect)
        await self.cache.mark_api_key_revoked(api_key_id)
        
        # Invalidate cache
        await self.cache.invalidate_api_key(api_key.key_hash)
    
    async def check_rate_limit(
        self,
        api_key_id: uuid.UUID,
        rate_limit_per_minute: int,
    ) -> bool:
        """
        Check if API key has exceeded rate limit.
        
        Args:
            api_key_id: API key ID
            rate_limit_per_minute: Maximum requests per minute
            
        Returns:
            True if under limit, False if exceeded
        """
        if rate_limit_per_minute <= 0:
            return True  # No limit
        
        key = f"ratelimit:apikey:{api_key_id}"
        count = await self.cache.increment_rate_limit(key, ttl_seconds=60)
        
        return count <= rate_limit_per_minute
    
    async def list_api_keys(
        self,
        org_id: uuid.UUID,
        include_inactive: bool = False,
    ) -> list[GSageAPIKey]:
        """
        List all API keys for an organization.
        
        Args:
            org_id: Organization ID
            include_inactive: Include revoked/inactive keys
            
        Returns:
            List of API key models (without raw keys!)
        """
        stmt = select(GSageAPIKey).where(
            GSageAPIKey.org_id == org_id,
        )
        
        if not include_inactive:
            stmt = stmt.where(GSageAPIKey.is_active)
        
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
    
    async def get_api_key(
        self,
        api_key_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> Optional[GSageAPIKey]:
        """
        Get API key by ID.
        
        Args:
            api_key_id: API key ID
            org_id: Organization ID (for validation)
            
        Returns:
            API key model or None
        """
        stmt = select(GSageAPIKey).where(
            GSageAPIKey.id == api_key_id,
            GSageAPIKey.org_id == org_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def rotate_api_key(
        self,
        api_key_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> tuple[str, GSageAPIKey]:
        """
        Rotate an API key: revoke old and create identical new one.

        This supports the key rotation threat mitigation from PROMPT.md.
        The new key inherits permissions, rate limit, and days remaining.

        Args:
            api_key_id: ID of the key to rotate
            org_id: Organization ID (for validation)

        Returns:
            Tuple of (raw_new_key, new_api_key_model)

        Raises:
            ValueError: If key not found or already revoked
        """
        old_key = await self.get_api_key(api_key_id, org_id)
        if not old_key:
            raise ValueError("API key not found")
        if old_key.revoked_at is not None:
            raise ValueError("Cannot rotate an already revoked API key")

        # Calculate remaining days (preserve original expiration window)
        remaining = old_key.expires_at - datetime.now(timezone.utc)
        days_remaining = max(1, remaining.days)

        # Revoke old key
        await self.revoke_api_key(api_key_id, org_id)

        # Create new key with same settings
        return await self.create_api_key(
            org_id=org_id,
            name=old_key.name,
            scoped_permissions=list(old_key.scoped_permissions),
            days_until_expiration=days_remaining,
            rate_limit_per_minute=old_key.rate_limit_per_minute,
        )


def get_api_key_service(
    session: AsyncSession,
    settings: Settings,
) -> APIKeyService:
    """
    Factory function to create APIKeyService.
    
    Args:
        session: Database session
        settings: Application settings
        
    Returns:
        APIKeyService instance
    """
    cache_service = get_cache_service(settings)
    return APIKeyService(session, cache_service)
