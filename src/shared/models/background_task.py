"""gSage AI — BackgroundTask model.

Tracks asynchronous tool executions dispatched to the Celery worker.

Status lifecycle::

    queued -> running -> completed
                     -> failed

Triggers::

    always_background   — ClassVar on the tool; dispatched immediately, never synced.
    pre_flight          — tool's should_run_background() returned True based on params.
    timeout_fallback    — synchronous execution timed out; re-dispatched to worker.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BackgroundTaskStatus(str):
    """Allowed values for GSageBackgroundTask.status."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BackgroundTaskTrigger(str):
    """What caused the tool to be dispatched to background."""

    ALWAYS_BACKGROUND = "always_background"
    PRE_FLIGHT = "pre_flight"
    TIMEOUT_FALLBACK = "timeout_fallback"


class GSageBackgroundTask(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Record of a single background tool execution.

    Bytes of actual result are stored in ``result`` (JSONB) — same shape as
    ``ToolResult.to_dict()``.  The row is never deleted; it serves as an
    audit trail and as the notification source for in-conversation injection.
    """

    __tablename__ = "gsage_background_tasks"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dept_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Department context when this task was dispatched.",
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # gSage session (= conversation) that triggered this task.
    # Used to scope in-conversation notifications to the originating session.
    gsage_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_tenant_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    tool_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    profile_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="default",
    )

    # Params passed to tool.execute() (framework params already stripped).
    tool_params: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    # Serialised AgentContext fields, used by the Celery worker to
    # reconstruct the context without a live request.
    agent_context_data: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    # Audit context extracted by BaseTool.run() for the Celery worker to
    # include in the Elasticsearch audit log.
    audit_context_data: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
    )

    # Celery task ID; populated after successful dispatch.
    celery_task_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    # What triggered background execution.
    trigger: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=BackgroundTaskTrigger.ALWAYS_BACKGROUND,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=BackgroundTaskStatus.QUEUED,
        index=True,
    )

    # ToolResult.to_dict() stored after completion.
    result: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Whether the user has been notified inside the conversation.
    # Prevents repeated notifications across multiple conversation turns.
    notified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        # Fast lookup of pending notifications for a given session.
        Index(
            "ix_bg_tasks_session_notify",
            "gsage_session_id",
            "notified",
            "status",
        ),
        # Fast cleanup of old records by org.
        Index(
            "ix_bg_tasks_org_created",
            "org_id",
            "created_at",
        ),
    )
