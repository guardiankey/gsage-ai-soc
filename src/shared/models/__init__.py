"""Shared SQLAlchemy model package used across all application services."""

from __future__ import annotations

# Base classes
from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Core models
from src.shared.models.organization import GSageOrganization
from src.shared.models.user import GSageUser, gsage_user_groups
from src.shared.models.user_organization import GSageUserOrganization
from src.shared.models.group import GSageGroup, gsage_group_permissions
from src.shared.models.permission import GSagePermission
from src.shared.models.api_key import GSageAPIKey

# Session / agent run models
from src.shared.models.tenant_session import GSageTenantSession
from src.shared.models.conversation_folder import GSageConversationFolder
from src.shared.models.agent_run import GSageAgentRun

# Tool models
from src.shared.models.tool import GSageTool
from src.shared.models.tool_config import GSageToolConfig
from src.shared.models.tool_state import GSageToolState
from src.shared.models.tool_cache import GSageToolCache, CacheScope
from src.shared.models.org_tool_settings import GSageOrgToolSettings

# Email models
from src.shared.models.email_account import GSageEmailAccount
from src.shared.models.email_message import GSageEmailMessage, GSageEmailDirection, GSageEmailStatus
from src.shared.models.email_thread import GSageEmailThread

# Generic channel models (Telegram, Discord, Slack, etc.)
from src.shared.models.channel_message import GSageChannelMessage, GSageChannelDirection, GSageChannelStatus
from src.shared.models.channel_conversation import GSageChannelConversation

# Knowledge base (stored in Weaviate)
from src.shared.models.knowledge_base import GSageKnowledgeSource

# Prompt library
from src.shared.models.prompt import (
    GSagePrompt,
    GSagePromptCategory,
    GSageUserPromptFavorite,
)

# Scheduled jobs
from src.shared.models.scheduled_job import (
    GSageScheduledJob,
    GSageScheduledJobType,
    GSageScheduledJobStatus,
)

# Approval delegation
from src.shared.models.approval_rule import GSageApprovalRule
from src.shared.models.approval_delegation import GSageApprovalDelegation

# Ingest jobs
from src.shared.models.ingest_job import GSageIngestJob, IngestScope, IngestStatus

# Tool-generated files stored in MinIO
from src.shared.models.generated_file import GSageFile

# OTP trusted devices
from src.shared.models.trusted_device import GSageTrustedDevice

# Background tool execution tasks
from src.shared.models.background_task import GSageBackgroundTask, BackgroundTaskStatus, BackgroundTaskTrigger

# Interface profiles (per-org, per-interface tool permission scoping)
from src.shared.models.interface_profile import GSageInterfaceProfile

# Dynamic data stores
from src.shared.models.datastore import GSageDataStore, GSageDataStoreRecord

# Departments
from src.shared.models.department import GSageDepartment
from src.shared.models.user_department import GSageUserDepartment, DepartmentRole

# SSO email domain → org mapping
from src.shared.models.org_email_domain import GSageOrgEmailDomain

# Per-user credentials keychain
from src.shared.models.user_credential import (
    GSageUserCredential,
    GSageUserCredentialToolLink,
    CredentialKind,
)

__all__ = [
    # Base
    "Base",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    # Core
    "GSageOrganization",
    "GSageUser",
    "GSageUserOrganization",
    "GSageGroup",
    "GSagePermission",
    "GSageAPIKey",
    "gsage_user_groups",
    "gsage_group_permissions",
    # Sessions / runs
    "GSageTenantSession",
    "GSageConversationFolder",
    "GSageAgentRun",
    # Tools
    "GSageTool",
    "GSageToolConfig",
    "GSageToolState",
    "GSageToolCache",
    "CacheScope",
    # Email
    "GSageEmailAccount",
    "GSageEmailMessage",
    "GSageEmailDirection",
    "GSageEmailStatus",
    "GSageEmailThread",
    # Generic channel messages
    "GSageChannelMessage",
    "GSageChannelDirection",
    "GSageChannelStatus",
    "GSageChannelConversation",
    # Knowledge base (stored in Weaviate)
    "GSageKnowledgeSource",
    # Scheduled jobs
    "GSageScheduledJob",
    "GSageScheduledJobType",
    "GSageScheduledJobStatus",
    # Approval delegation
    "GSageApprovalRule",
    "GSageApprovalDelegation",
    # Ingest jobs
    "GSageIngestJob",
    "IngestScope",
    "IngestStatus",
    # Interface profiles
    "GSageInterfaceProfile",
    # Background tasks
    "GSageBackgroundTask",
    "BackgroundTaskStatus",
    "BackgroundTaskTrigger",
    # Dynamic data stores
    "GSageDataStore",
    "GSageDataStoreRecord",
    # Departments
    "GSageDepartment",
    "GSageUserDepartment",
    "DepartmentRole",
    # SSO email domain mapping
    "GSageOrgEmailDomain",
    # User credentials keychain
    "GSageUserCredential",
    "GSageUserCredentialToolLink",
    "CredentialKind",
]
