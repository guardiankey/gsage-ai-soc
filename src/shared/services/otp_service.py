"""gSage AI — OTP service (TOTP RFC 6238) and policy resolution."""

from __future__ import annotations

import base64
import enum
import hashlib
import io
import json
import secrets
from typing import Optional, TYPE_CHECKING

import bcrypt
import pyotp
import qrcode
import qrcode.image.pil

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.user import GSageUser


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class OTPRequirement(enum.Enum):
    """Result of resolve_otp_requirement()."""

    SKIP = "skip"               # OTP not required (disabled / whitelisted / trusted device)
    REQUIRED = "required"       # User must verify OTP
    NOT_ENROLLED = "not_enrolled"  # Policy requires OTP but user has no secret yet


class OTPService:
    """TOTP generation, verification, backup codes and device token utilities."""

    # ------------------------------------------------------------------
    # Secret generation
    # ------------------------------------------------------------------

    @staticmethod
    def generate_secret() -> str:
        """Return a random base32 TOTP secret (compatible with all authenticator apps)."""
        return pyotp.random_base32()

    @staticmethod
    def get_provisioning_uri(secret: str, email: str, issuer: str = "gSage AI") -> str:
        """Return the otpauth:// URI used to configure an authenticator app."""
        totp = pyotp.TOTP(secret)
        return totp.provisioning_uri(name=email, issuer_name=issuer)

    @staticmethod
    def generate_qr_base64(uri: str) -> str:
        """Return a PNG QR code of the provisioning URI as a base64 data-URI string."""
        img = qrcode.make(uri, image_factory=qrcode.image.pil.PilImage)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{b64}"

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    @staticmethod
    def verify_totp(secret: str, code: str) -> bool:
        """Verify a 6-digit TOTP code. Allows ±1 window (30s tolerance)."""
        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=1)

    # ------------------------------------------------------------------
    # Backup codes
    # ------------------------------------------------------------------

    @staticmethod
    def generate_backup_codes(count: int = 10) -> tuple[list[str], list[str]]:
        """Generate backup codes.

        Returns:
            (plaintext_list, bcrypt_hashed_list) — store hashes, show plaintext once.
        """
        plaintext = [secrets.token_hex(4).upper() + "-" + secrets.token_hex(4).upper() for _ in range(count)]
        hashed = [bcrypt.hashpw(code.encode(), bcrypt.gensalt()).decode() for code in plaintext]
        return plaintext, hashed

    @staticmethod
    def verify_backup_code(code: str, hashed_codes: list[str]) -> tuple[bool, list[str]]:
        """Verify a backup code against the stored list and consume it if matched.

        Returns:
            (matched, remaining_hashes) — remaining_hashes excludes the consumed code.
        """
        for hashed in hashed_codes:
            if bcrypt.checkpw(code.encode(), hashed.encode()):
                remaining = [h for h in hashed_codes if h != hashed]
                return True, remaining
        return False, hashed_codes

    # ------------------------------------------------------------------
    # Device tokens
    # ------------------------------------------------------------------

    @staticmethod
    def generate_device_token() -> str:
        """Generate a cryptographically random device token (64 hex chars)."""
        return secrets.token_hex(32)

    @staticmethod
    def hash_device_token(token: str) -> str:
        """Return the SHA-256 hex-digest of a device token (for DB storage)."""
        return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------

def _get_otp_config(org: GSageOrganization) -> dict:
    """Extract the OTP sub-config from the org's auth_config."""
    return org.auth_config.get("otp", {})


def _is_trusted_network(client_ip: str, trusted_networks: list[str]) -> bool:
    """Return True if client_ip belongs to any network in trusted_networks (CIDR or exact)."""
    if not client_ip or not trusted_networks:
        return False
    import ipaddress
    try:
        addr = ipaddress.ip_address(client_ip)
        for cidr in trusted_networks:
            try:
                if addr in ipaddress.ip_network(cidr, strict=False):
                    return True
            except ValueError:
                continue
    except ValueError:
        pass
    return False


async def resolve_otp_requirement(
    org: GSageOrganization,
    user: GSageUser,
    client_ip: Optional[str],
    device_token: Optional[str],
    db: AsyncSession,
) -> OTPRequirement:
    """Determine whether OTP verification is required for this login attempt.

    Policy values:
        "disabled"  — OTP never asked
        "optional"  — OTP asked only if user has enrolled (default)
        "required"  — OTP always asked; users without enrollment get NOT_ENROLLED

    Bypass conditions (checked in order):
        1. API keys are already exempt at the call site (not handled here)
        2. Policy == "disabled"
        3. Client IP is in the org's trusted_networks list
        4. A valid (non-expired) trusted device record exists for the device_token

    Returns:
        OTPRequirement enum value
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from src.shared.models.trusted_device import GSageTrustedDevice

    otp_config = _get_otp_config(org)
    policy: str = otp_config.get("policy", "optional")

    # 1. OTP disabled for this org
    if policy == "disabled":
        return OTPRequirement.SKIP

    # 2. Trusted network bypass
    trusted_networks: list[str] = otp_config.get("trusted_networks", [])
    if _is_trusted_network(client_ip or "", trusted_networks):
        return OTPRequirement.SKIP

    # 3. Trusted device bypass
    if device_token:
        device_hash = OTPService.hash_device_token(device_token)
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(GSageTrustedDevice).where(
                GSageTrustedDevice.user_id == user.id,
                GSageTrustedDevice.device_hash == device_hash,
                GSageTrustedDevice.expires_at > now,
            )
        )
        if result.scalar_one_or_none():
            return OTPRequirement.SKIP

    # 4. Check enrollment status
    if user.otp_enabled and user.otp_secret:
        return OTPRequirement.REQUIRED

    # User not enrolled
    if policy == "required":
        return OTPRequirement.NOT_ENROLLED

    # policy == "optional" and user not enrolled → skip
    return OTPRequirement.SKIP
