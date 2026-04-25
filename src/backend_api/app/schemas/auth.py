"""gSage AI — authentication schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, field_validator


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    must_change_password: bool = False
    # OTP two-step flow fields
    otp_required: bool = False
    otp_token: Optional[str] = None
    otp_not_enrolled: bool = False
    device_token: Optional[str] = None


class RefreshRequest(BaseModel):
    refresh_token: str


class LoginRequest(BaseModel):
    """JSON-body login (alternative to OAuth2 form for programmatic clients)."""

    email: EmailStr
    password: str
    # Optional: choose which org to obtain a token for.
    # When omitted the user's first active org is used.
    org_id: Optional[uuid.UUID] = None


class RegisterRequest(BaseModel):
    """Register a new user and create their first organization (owner role)."""

    email: EmailStr
    password: str
    full_name: str
    org_name: str
    # Slug is auto-derived from org_name when omitted.
    org_slug: Optional[str] = None

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

    @field_validator("org_slug")
    @classmethod
    def slug_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        import re
        if not re.match(r"^[a-z0-9-]{2,100}$", v):
            raise ValueError("Slug must be 2–100 lowercase alphanumeric chars or hyphens")
        return v


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class OrgMembershipOut(BaseModel):
    org_id: uuid.UUID
    org_name: str
    org_slug: str
    role: str
    is_active: bool
    permissions: list[str] = []
    departments: list["DepartmentMembershipOut"] = []


class DepartmentMembershipOut(BaseModel):
    dept_id: uuid.UUID
    dept_name: str
    dept_slug: str
    role: str
    is_active: bool

    model_config = {"from_attributes": True}


class MeResponse(UserOut):
    memberships: list[OrgMembershipOut] = []


class UpdateProfileRequest(BaseModel):
    full_name: str

    @field_validator("full_name")
    @classmethod
    def full_name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("full_name cannot be empty")
        return v


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


# ---------------------------------------------------------------------------
# OTP schemas
# ---------------------------------------------------------------------------

class OTPVerifyRequest(BaseModel):
    """Verify OTP code after login (two-step flow)."""
    otp_token: str
    code: str
    remember_device: bool = False
    user_agent: Optional[str] = None


class OTPSetupResponse(BaseModel):
    """Returned when initiating OTP enrollment."""
    secret: str
    provisioning_uri: str
    qr_code: str  # base64 data URI


class OTPConfirmRequest(BaseModel):
    """Confirm OTP enrollment with the first valid code."""
    code: str


class OTPConfirmResponse(BaseModel):
    """Returned after confirming OTP enrollment — includes backup codes (show once)."""
    backup_codes: list[str]


class OTPDisableRequest(BaseModel):
    """Disable OTP — requires current password for local accounts."""
    password: Optional[str] = None
    code: Optional[str] = None


class OTPStatusResponse(BaseModel):
    """Current OTP status for the authenticated user."""
    otp_enabled: bool
    otp_confirmed_at: Optional[datetime] = None
    backup_codes_count: Optional[int] = None


# ---------------------------------------------------------------------------
# Org OTP config schemas
# ---------------------------------------------------------------------------

class OrgOTPConfigResponse(BaseModel):
    """Current OTP configuration for an organization."""
    policy: str = "optional"   # "disabled" | "optional" | "required"
    trusted_networks: list[str] = []
    remember_device_days: int = 30
    issuer_name: str = "gSage AI"


class OrgOTPConfigRequest(BaseModel):
    """Update OTP configuration for an organization."""
    policy: Optional[str] = None
    trusted_networks: Optional[list[str]] = None
    remember_device_days: Optional[int] = None
    issuer_name: Optional[str] = None

    @field_validator("policy")
    @classmethod
    def policy_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("disabled", "optional", "required"):
            raise ValueError("policy must be 'disabled', 'optional', or 'required'")
        return v

    @field_validator("remember_device_days")
    @classmethod
    def days_range(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not (1 <= v <= 365):
            raise ValueError("remember_device_days must be between 1 and 365")
        return v
