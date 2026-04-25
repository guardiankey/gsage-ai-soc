"""gSage AI — Admin module schemas.

Covers: Organization, Users, Groups, Permissions, ToolConfigs,
InterfaceProfiles, and EmailAccounts management.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _slug_validator(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    v = v.strip().lower()
    if not re.match(r"^[a-z0-9-]{2,100}$", v):
        raise ValueError("slug must be 2–100 lowercase alphanumeric chars or hyphens")
    return v


# ---------------------------------------------------------------------------
# Organization schemas
# ---------------------------------------------------------------------------

class OrganizationOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    system_prompt: Optional[str]
    default_maker_model: str
    default_reviewer_model: str
    agent_timeout_seconds: int
    max_context_tokens: int
    llm_provider: str
    # API key masked — only show whether it is set
    llm_api_key_set: bool
    auth_providers: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class OrganizationUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    is_active: Optional[bool] = None
    system_prompt: Optional[str] = Field(default=None, max_length=5000)
    default_maker_model: Optional[str] = Field(default=None, max_length=100)
    default_reviewer_model: Optional[str] = Field(default=None, max_length=100)
    agent_timeout_seconds: Optional[int] = Field(default=None, ge=10, le=300)
    max_context_tokens: Optional[int] = Field(default=None, ge=1000, le=128000)
    llm_provider: Optional[str] = Field(default=None, pattern=r"^(ollama|openai|deepseek|anthropic|gemini)$")
    # Providing a value sets the key; providing null clears it
    llm_api_key: Optional[str] = None

    @field_validator("slug")
    @classmethod
    def slug_fmt(cls, v: Optional[str]) -> Optional[str]:
        return _slug_validator(v)

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name cannot be empty")
        return v


# ---------------------------------------------------------------------------
# Permission schemas
# ---------------------------------------------------------------------------

class PermissionOut(BaseModel):
    id: uuid.UUID
    tag: str
    description: Optional[str]
    category: str

    model_config = {"from_attributes": True}


class GroupPermissionOut(BaseModel):
    """A permission assigned to a group, optionally scoped to a department."""
    id: uuid.UUID
    tag: str
    description: Optional[str]
    category: str
    dept_id: Optional[uuid.UUID] = None  # None = global (all departments)

    model_config = {"from_attributes": True}


class GroupPermissionEntry(BaseModel):
    """Single entry in a group-permission assignment request."""
    permission_id: uuid.UUID
    dept_id: Optional[uuid.UUID] = None  # None = global (all departments)


# ---------------------------------------------------------------------------
# Group schemas
# ---------------------------------------------------------------------------

class GroupMemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str

    model_config = {"from_attributes": True}


class GroupOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    description: Optional[str]
    member_count: int
    permission_tags: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class GroupDetail(GroupOut):
    members: list[GroupMemberOut] = []
    permissions: list[GroupPermissionOut] = []


class GroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)


class GroupUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)


class GroupMembersUpdate(BaseModel):
    """Replace the full member list for a group."""
    user_ids: list[uuid.UUID]


class GroupPermissionsUpdate(BaseModel):
    """Replace the full permission list for a group (global + all dept-scoped)."""
    permissions: list[GroupPermissionEntry]


# ---------------------------------------------------------------------------
# User (admin management) schemas
# ---------------------------------------------------------------------------

class AdminUserDeptAssignment(BaseModel):
    dept_id: uuid.UUID
    role: str = Field(default="member", pattern=r"^(admin|member|viewer)$")


class AdminUserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    auth_provider: str
    otp_enabled: bool
    role_in_org: str
    group_ids: list[uuid.UUID]
    dept_ids: list[uuid.UUID]
    telegram_id: Optional[str] = None
    secondary_emails: Optional[str] = None
    ai_instructions: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AdminUserDetail(AdminUserOut):
    """Extended user detail including group and department info."""
    groups: list[GroupOut] = []
    departments: list[dict] = []  # {dept_id, dept_name, role}


class AdminUserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8)
    role: str = Field(default="member", pattern=r"^(owner|admin|member|viewer)$")

    @field_validator("full_name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("full_name cannot be empty")
        return v


class AdminUserUpdate(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    is_active: Optional[bool] = None
    role: Optional[str] = Field(default=None, pattern=r"^(owner|admin|member|viewer)$")
    telegram_id: Optional[str] = Field(default=None, max_length=50)
    secondary_emails: Optional[str] = None
    ai_instructions: Optional[str] = None
    otp_enabled: Optional[bool] = None


class AdminUserGroupsUpdate(BaseModel):
    """Replace all group memberships for a user."""
    group_ids: list[uuid.UUID]


class AdminUserDepartmentsUpdate(BaseModel):
    """Replace all department memberships for a user."""
    assignments: list[AdminUserDeptAssignment]


class ResetPasswordResponse(BaseModel):
    temporary_password: str
    message: str = "Password reset successfully. The user must change it on next login."


# ---------------------------------------------------------------------------
# ToolConfig schemas
# ---------------------------------------------------------------------------

class ToolConfigOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    dept_id: Optional[uuid.UUID]
    tool_name: str
    profile_id: str
    description: Optional[str]
    config: Dict[str, Any]  # Decrypted for admin view
    updated_by_user_id: Optional[uuid.UUID]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ToolConfigCreate(BaseModel):
    dept_id: Optional[uuid.UUID] = None
    tool_name: str = Field(..., min_length=1, max_length=100)
    profile_id: str = Field(default="default", min_length=1, max_length=100)
    description: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)


class ToolConfigUpdate(BaseModel):
    tool_name: Optional[str] = None
    profile_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    dept_id: Optional[uuid.UUID] = None
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# InterfaceProfile schemas
# ---------------------------------------------------------------------------

class InterfaceProfileOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    dept_id: Optional[uuid.UUID]
    interface: str
    user_id: Optional[uuid.UUID]
    is_active: bool
    description: Optional[str]
    system_prompt: Optional[str]
    mode: str
    tool_permissions: list[str]
    interface_config: Optional[Dict[str, Any]]
    preferences: Optional[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class InterfaceProfileCreate(BaseModel):
    dept_id: Optional[uuid.UUID] = None
    interface: str = Field(..., pattern=r"^(web|email|telegram|whatsapp|slack|api|cli)$")
    user_id: Optional[uuid.UUID] = None
    is_active: bool = True
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    mode: str = Field(default="denylist", pattern=r"^(allowlist|denylist)$")
    tool_permissions: list[str] = Field(default_factory=list)
    interface_config: Optional[Dict[str, Any]] = None
    preferences: Optional[Dict[str, Any]] = None


class InterfaceProfileUpdate(BaseModel):
    dept_id: Optional[uuid.UUID] = None
    user_id: Optional[uuid.UUID] = None
    is_active: Optional[bool] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    mode: Optional[str] = Field(default=None, pattern=r"^(allowlist|denylist)$")
    tool_permissions: Optional[list[str]] = None
    interface_config: Optional[Dict[str, Any]] = None
    preferences: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# EmailAccount schemas
# ---------------------------------------------------------------------------

class EmailAccountOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    dept_id: Optional[uuid.UUID]
    display_name: str
    email: str
    is_active: bool
    imap_host: str
    imap_port: int
    imap_use_tls: bool
    imap_verify_ssl: bool
    imap_username: str
    imap_password_set: bool  # Only expose whether password is set, not the value
    imap_folder: str
    imap_idle_supported: bool
    smtp_host: str
    smtp_port: int
    smtp_use_tls: bool
    smtp_verify_ssl: bool
    smtp_username: str
    smtp_password_set: bool  # Only expose whether password is set, not the value
    sender_name: str
    subject_prefix: Optional[str]
    reply_footer: Optional[str]
    unknown_sender_folder: str
    max_email_size_bytes: int
    polling_interval_seconds: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EmailAccountCreate(BaseModel):
    dept_id: Optional[uuid.UUID] = None
    display_name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    is_active: bool = True
    imap_host: str = Field(..., min_length=1, max_length=255)
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_use_tls: bool = True
    imap_verify_ssl: bool = True
    imap_username: str = Field(..., min_length=1, max_length=255)
    imap_password: str = Field(..., min_length=1)
    imap_folder: str = Field(default="INBOX", max_length=100)
    imap_idle_supported: bool = True
    smtp_host: str = Field(..., min_length=1, max_length=255)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_use_tls: bool = True
    smtp_verify_ssl: bool = True
    smtp_username: str = Field(default="", max_length=255)
    smtp_password: Optional[str] = None  # None = unauthenticated relay
    sender_name: str = Field(default="SOC AI Assistant", max_length=255)
    subject_prefix: Optional[str] = Field(default=None, max_length=50)
    reply_footer: Optional[str] = None
    unknown_sender_folder: str = Field(default="Unknown-Senders", max_length=100)
    max_email_size_bytes: int = Field(default=5242880, ge=1024)
    polling_interval_seconds: int = Field(default=60, ge=10, le=3600)


class EmailAccountUpdate(BaseModel):
    dept_id: Optional[uuid.UUID] = None
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    is_active: Optional[bool] = None
    imap_host: Optional[str] = Field(default=None, min_length=1, max_length=255)
    imap_port: Optional[int] = Field(default=None, ge=1, le=65535)
    imap_use_tls: Optional[bool] = None
    imap_verify_ssl: Optional[bool] = None
    imap_username: Optional[str] = Field(default=None, min_length=1, max_length=255)
    imap_password: Optional[str] = None  # Only update if provided
    imap_folder: Optional[str] = Field(default=None, max_length=100)
    imap_idle_supported: Optional[bool] = None
    smtp_host: Optional[str] = Field(default=None, min_length=1, max_length=255)
    smtp_port: Optional[int] = Field(default=None, ge=1, le=65535)
    smtp_use_tls: Optional[bool] = None
    smtp_verify_ssl: Optional[bool] = None
    smtp_username: Optional[str] = Field(default=None, min_length=1, max_length=255)
    smtp_password: Optional[str] = None  # Only update if provided
    sender_name: Optional[str] = Field(default=None, max_length=255)
    subject_prefix: Optional[str] = Field(default=None, max_length=50)
    reply_footer: Optional[str] = None
    unknown_sender_folder: Optional[str] = Field(default=None, max_length=100)
    max_email_size_bytes: Optional[int] = Field(default=None, ge=1024)
    polling_interval_seconds: Optional[int] = Field(default=None, ge=10, le=3600)


class EmailConnectionTestResult(BaseModel):
    imap_ok: bool
    smtp_ok: bool
    imap_error: Optional[str] = None
    smtp_error: Optional[str] = None
