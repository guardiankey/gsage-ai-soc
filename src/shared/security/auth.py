"""gSage AI — Authentication utilities."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt as _bcrypt
import jwt as _jwt


def hash_password(password: str) -> str:
    """
    Hash a password using bcrypt.

    Args:
        password: Plain text password

    Returns:
        Hashed password string
    """
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against a hash.

    Args:
        plain_password: Plain text password to verify
        hashed_password: Stored password hash

    Returns:
        True if password matches, False otherwise
    """
    return _bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


def generate_api_key(environment: str = "live") -> tuple[str, str, str]:
    """
    Generate a new API key and its SHA-256 hash.

    Args:
        environment: Key environment ('live' or 'test')

    Returns:
        Tuple of (raw_key, key_hash, key_prefix)
        - raw_key: Show this to user ONCE (e.g., gk_live_N7x2...)
        - key_hash: SHA-256 digest — store this in the database
        - key_prefix: First 12 chars for fast UI display (e.g., gk_live_N7x2)

    Format: gk_{environment}_{32 random bytes in base64url}
    Example: gk_live_N7x2K9mP4vQ8wBzC5tYhJ6uR3sL1nDfG
    """
    random_bytes = secrets.token_urlsafe(32)
    raw_key = f"gk_{environment}_{random_bytes}"
    key_prefix = raw_key[:12]
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    return raw_key, key_hash, key_prefix


def hash_api_key(raw_key: str) -> str:
    """
    Hash an API key for lookup.
    
    Args:
        raw_key: The raw API key (e.g., gk_...)
        
    Returns:
        SHA-256 hash of the key
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()


def validate_api_key_format(api_key: str) -> bool:
    """
    Validate API key format.

    Args:
        api_key: API key to validate

    Returns:
        True if format is valid (gk_live_... or gk_test_...)
    """
    if not api_key:
        return False

    # Must start with gk_live_ or gk_test_
    if not (api_key.startswith("gk_live_") or api_key.startswith("gk_test_")):
        return False

    # Must have reasonable length
    if len(api_key) < 40 or len(api_key) > 70:
        return False

    return True


def generate_csrf_token() -> str:
    """
    Generate a CSRF token.
    
    Returns:
        URL-safe random token (32 bytes)
    """
    return secrets.token_urlsafe(32)


def is_api_key_expired(
    expires_at: datetime,
    now: Optional[datetime] = None
) -> bool:
    """
    Check if an API key has expired.
    
    Args:
        expires_at: Expiration datetime
        now: Current datetime (defaults to utcnow)
        
    Returns:
        True if expired
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return now >= expires_at


def generate_session_token() -> str:
    """
    Generate a secure session token.
    
    Returns:
        URL-safe random token (32 bytes)
    """
    return secrets.token_urlsafe(32)


def calculate_api_key_expiration(years: int = 1) -> datetime:
    """
    Calculate API key expiration date.
    
    Args:
        years: Number of years until expiration (max: 1)
        
    Returns:
        Expiration datetime
        
    Raises:
        ValueError: If years > 1
    """
    if years > 1:
        raise ValueError("API key expiration cannot exceed 1 year")
    
    return datetime.now(timezone.utc) + timedelta(days=365 * years)


# ── JWT ──────────────────────────────────────────────────────────────────────

def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Create a signed JWT access token.

    Args:
        data: Claims to encode (should include 'sub' with user ID).
        expires_delta: Custom TTL; defaults to jwt_access_token_expire_minutes.

    Returns:
        Signed JWT string.
    """
    from src.shared.config.settings import get_settings
    settings = get_settings()
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.jwt_access_token_expire_minutes)
    )
    to_encode["exp"] = expire
    to_encode.setdefault("type", "access")
    to_encode.setdefault("iss", "gsage")
    return _jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(data: dict) -> str:
    """Create a signed JWT refresh token with a longer TTL.

    Args:
        data: Claims to encode (should include 'sub' with user ID).

    Returns:
        Signed JWT string.
    """
    from src.shared.config.settings import get_settings
    settings = get_settings()
    to_encode = data.copy()
    to_encode["exp"] = datetime.now(timezone.utc) + timedelta(
        days=settings.jwt_refresh_token_expire_days
    )
    to_encode["type"] = "refresh"
    return _jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """Decode and verify a JWT token.

    Args:
        token: Encoded JWT string.

    Returns:
        Decoded payload dict.

    Raises:
        ValueError: If the token is expired or invalid.
    """
    from src.shared.config.settings import get_settings
    settings = get_settings()
    try:
        payload: dict = _jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        return payload
    except _jwt.ExpiredSignatureError as exc:
        raise ValueError("Token has expired") from exc
    except _jwt.InvalidTokenError as exc:
        raise ValueError(f"Invalid token: {exc}") from exc
