"""gSage AI — Permission resolution service."""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config.settings import Settings
from src.shared.security.permissions import (
    filter_permissions_by_scope,
    resolve_user_permissions,
)
from src.shared.services.cache_service import CacheService, get_cache_service


class PermissionService:
    """Service for resolving RBAC permissions with Redis caching."""
    
    def __init__(
        self,
        session: AsyncSession,
        cache_service: CacheService,
    ):
        """
        Initialize permission service.
        
        Args:
            session: Database session
            cache_service: Cache service for Redis operations
        """
        self.session = session
        self.cache = cache_service
    
    async def get_user_permissions(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        api_key_scoped_permissions: Optional[list[str]] = None,
    ) -> tuple[list[uuid.UUID], list[str]]:
        """
        Get user's permissions with optional API key scope filtering.
        
        Uses Redis cache with 5-minute TTL per PROMPT.md spec.
        
        Args:
            user_id: User ID
            org_id: Organization ID
            api_key_scoped_permissions: Optional API key scope to filter by
            
        Returns:
            Tuple of (group_ids, permission_tags)
            
        Example:
            group_ids, permissions = await service.get_user_permissions(
                user_id=uuid.UUID("..."),
                org_id=uuid.UUID("..."),
                api_key_scoped_permissions=["dns:read", "whois:read"],
            )
        """
        # Try cache first
        cached = await self.cache.get_cached_permissions(user_id, org_id)
        
        if cached is None:
            # Cache miss - resolve from database
            group_ids, permission_tags = await resolve_user_permissions(
                self.session,
                user_id,
                org_id,
            )
            
            # Cache the result
            await self.cache.set_cached_permissions(
                user_id,
                org_id,
                group_ids,
                permission_tags,
            )
        else:
            # Cache hit
            group_ids, permission_tags = cached
        
        # Apply API key scope filtering if provided
        if api_key_scoped_permissions is not None:
            permission_tags = filter_permissions_by_scope(
                permission_tags,
                api_key_scoped_permissions,
            )
        
        return group_ids, permission_tags
    
    async def invalidate_user_cache(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> None:
        """
        Invalidate cached permissions for a user.
        
        Call this when:
        - User is added/removed from a group
        - User's groups are modified
        
        Args:
            user_id: User ID
            org_id: Organization ID
        """
        await self.cache.invalidate_user_permissions(user_id, org_id)
    
    async def invalidate_group_cache(
        self,
        group_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> None:
        """
        Invalidate cached permissions for all users in a group.
        
        Call this when:
        - Group permissions are modified
        - Permissions are added/removed from a group
        
        Args:
            group_id: Group ID
            org_id: Organization ID
        """
        await self.cache.invalidate_group_permissions(group_id, org_id)
    
    async def check_permission(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        required_permission: str,
        api_key_scoped_permissions: Optional[list[str]] = None,
    ) -> bool:
        """
        Check if user has a specific permission.
        
        Args:
            user_id: User ID
            org_id: Organization ID
            required_permission: Permission tag to check (e.g., "dns:read")
            api_key_scoped_permissions: Optional API key scope
            
        Returns:
            True if user has the permission
        """
        _, permissions = await self.get_user_permissions(
            user_id,
            org_id,
            api_key_scoped_permissions,
        )
        
        return required_permission in permissions
    
    async def check_any_permission(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        required_permissions: list[str],
        api_key_scoped_permissions: Optional[list[str]] = None,
    ) -> bool:
        """
        Check if user has ANY of the specified permissions (OR).
        
        Args:
            user_id: User ID
            org_id: Organization ID
            required_permissions: List of permission tags
            api_key_scoped_permissions: Optional API key scope
            
        Returns:
            True if user has at least one permission
        """
        _, permissions = await self.get_user_permissions(
            user_id,
            org_id,
            api_key_scoped_permissions,
        )
        
        return any(perm in permissions for perm in required_permissions)
    
    async def check_all_permissions(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        required_permissions: list[str],
        api_key_scoped_permissions: Optional[list[str]] = None,
    ) -> bool:
        """
        Check if user has ALL of the specified permissions (AND).
        
        Args:
            user_id: User ID
            org_id: Organization ID
            required_permissions: List of permission tags
            api_key_scoped_permissions: Optional API key scope
            
        Returns:
            True if user has all permissions
        """
        _, permissions = await self.get_user_permissions(
            user_id,
            org_id,
            api_key_scoped_permissions,
        )
        
        permission_set = set(permissions)
        return all(perm in permission_set for perm in required_permissions)


def get_permission_service(
    session: AsyncSession,
    settings: Settings,
) -> PermissionService:
    """
    Factory function to create PermissionService.
    
    Args:
        session: Database session
        settings: Application settings
        
    Returns:
        PermissionService instance
    """
    cache_service = get_cache_service(settings)
    return PermissionService(session, cache_service)
