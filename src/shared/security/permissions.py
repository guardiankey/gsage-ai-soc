"""gSage AI — RBAC permission resolution."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.shared.models import (
    GSageAPIKey,
    GSageGroup,
    GSageUser,
    GSageUserOrganization,
)


async def resolve_user_permissions(
    session: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
) -> tuple[list[uuid.UUID], list[str]]:
    """
    Resolve user's group IDs and permission tags from RBAC.
    
    Args:
        session: Database session
        user_id: User ID
        org_id: Organization ID (for validation)
        
    Returns:
        Tuple of (group_ids, permission_tags)
        
    Example:
        group_ids = [uuid1, uuid2]
        permission_tags = ["dns:read", "whois:read", "decode:base64"]
    """
    # Load user with groups and their permissions
    stmt = (
        select(GSageUser)
        .join(
            GSageUserOrganization,
            (GSageUserOrganization.user_id == GSageUser.id)
            & (GSageUserOrganization.org_id == org_id)
            & GSageUserOrganization.is_active,
        )
        .where(
            GSageUser.id == user_id,
            GSageUser.is_active,
        )
        .options(
            selectinload(GSageUser.groups).selectinload(GSageGroup.permissions)
        )
    )
    
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    
    if not user:
        return [], []
    
    # Extract group IDs
    group_ids = [group.id for group in user.groups]
    
    # Extract unique permission tags from all groups
    permission_set = set()
    for group in user.groups:
        for permission in group.permissions:
            permission_set.add(permission.tag)
    
    permission_tags = sorted(permission_set)  # Sort for consistency
    
    return group_ids, permission_tags


async def resolve_api_key_permissions(
    session: AsyncSession,
    key_hash: str,
) -> tuple[uuid.UUID, list[str], uuid.UUID]:
    """
    Resolve API key's organization and scoped permissions.
    
    Args:
        session: Database session
        key_hash: SHA-256 hash of the API key
        
    Returns:
        Tuple of (org_id, scoped_permissions, api_key_id)
        
    Raises:
        ValueError: If API key not found, expired, inactive, or revoked
    """
    stmt = select(GSageAPIKey).where(
        GSageAPIKey.key_hash == key_hash,
        GSageAPIKey.is_active,
    )
    
    result = await session.execute(stmt)
    api_key = result.scalar_one_or_none()
    
    if not api_key:
        raise ValueError("API key not found or inactive")
    
    # Check expiration
    if datetime.now(timezone.utc) >= api_key.expires_at:
        raise ValueError("API key has expired")
    
    # Check if revoked
    if api_key.revoked_at is not None:
        raise ValueError("API key has been revoked")
    
    # Update last_used_at (fire and forget)
    api_key.last_used_at = datetime.now(timezone.utc)
    await session.commit()
    
    # Return org_id, scoped permissions, and api_key_id
    return api_key.org_id, api_key.scoped_permissions, api_key.id


def filter_permissions_by_scope(
    user_permissions: list[str],
    api_key_scoped_permissions: list[str],
) -> list[str]:
    """
    Filter user permissions by API key scope.
    
    API keys can have a subset of the organization's permissions.
    The effective permissions are the intersection of user permissions
    and API key scoped permissions.
    
    Args:
        user_permissions: User's full permission set
        api_key_scoped_permissions: API key's allowed permissions
            Use ["*"] to grant all user permissions (recommended for dev/admin keys).
        
    Returns:
        Filtered permission list (intersection)
        
    Example:
        user_permissions = ["dns:read", "whois:read", "admin:write"]
        api_key_scoped_permissions = ["dns:read", "whois:read"]
        result = ["dns:read", "whois:read"]

        # Wildcard — inherits all user permissions:
        api_key_scoped_permissions = ["*"]
        result = ["admin:write", "dns:read", "whois:read"]
    """
    # Wildcard: API key inherits all user permissions
    if api_key_scoped_permissions == ["*"]:
        return sorted(user_permissions)

    user_set = set(user_permissions)
    scope_set = set(api_key_scoped_permissions)
    
    # Intersection
    effective_permissions = user_set & scope_set
    
    return sorted(effective_permissions)


async def validate_permission_tags(
    session: AsyncSession,
    permission_tags: list[str],
) -> bool:
    """
    Validate that all permission tags exist in the system.
    
    Args:
        session: Database session
        permission_tags: List of permission tags to validate
        
    Returns:
        True if all tags are valid
        
    Raises:
        ValueError: If any tag is invalid
    """
    from src.shared.models import GSagePermission

    if not permission_tags:
        return True

    # Wildcard token — grants all of the user's effective permissions at
    # runtime via filter_permissions_by_scope(); not a real DB tag.
    if permission_tags == ["*"]:
        return True

    # Query all valid tags
    stmt = select(GSagePermission.tag)
    result = await session.execute(stmt)
    valid_tags = {row[0] for row in result}
    
    # Check if all requested tags exist
    invalid_tags = set(permission_tags) - valid_tags
    
    if invalid_tags:
        raise ValueError(f"Invalid permission tags: {', '.join(invalid_tags)}")
    
    return True


def permission_cache_key(user_id: uuid.UUID, org_id: uuid.UUID) -> str:
    """
    Generate Redis cache key for user permissions.
    
    Args:
        user_id: User ID
        org_id: Organization ID
        
    Returns:
        Cache key string
        
    Format: cache:permissions:{org_id}:{user_id}
    """
    return f"cache:permissions:{org_id}:{user_id}"


def api_key_cache_key(key_hash: str) -> str:
    """
    Generate Redis cache key for API key.
    
    Args:
        key_hash: SHA-256 hash of the API key
        
    Returns:
        Cache key string
        
    Format: cache:apikey:{key_hash}
    """
    return f"cache:apikey:{key_hash}"


def revoked_api_key_cache_key(api_key_id: uuid.UUID) -> str:
    """
    Generate Redis cache key for revoked API key check.
    
    Args:
        api_key_id: API key ID
        
    Returns:
        Cache key string
        
    Format: apikey:revoked:{api_key_id}
    """
    return f"apikey:revoked:{api_key_id}"
