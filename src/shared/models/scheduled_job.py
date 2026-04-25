"""gSage AI — Scheduled Job model.

A single table unifies two job types:

  PROMPT_RUN   — runs a user-defined prompt through the full LangGraph agent
                 pipeline, in the context of the owning user (their permissions).
  SYSTEM_TASK  — invokes an internal Celery task by name (maintenance, etc.).

Scheduling is driven by RedBeat (Redis-backed Celery Beat scheduler). Every
activate/update/deactivate on this model must be reflected in RedBeat via
ScheduledJobService.sync_to_redbeat() / remove_from_redbeat().

Lifecycle fields:
  starts_at / ends_at  — closed window: job is silently skipped outside this range.
  is_active            — manual kill-switch (disable without deleting).
  max_runs             — auto-deactivate after N executions (None = unlimited).
  run_count            — incremented atomically on each successful execution.

All Celery-side execution goes through the single task
  src.backend.app.workers.tasks.scheduled_job.run_scheduled_job(job_id=...)
which loads the row, validates the window, and dispatches.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.user import GSageUser


class GSageScheduledJobType(str, enum.Enum):
    """Discriminator for the two job flavours."""

    PROMPT_RUN = "PROMPT_RUN"
    """Run a user prompt through the agent with the owner's permissions."""

    SYSTEM_TASK = "SYSTEM_TASK"
    """Invoke an internal Celery task by its fully-qualified task name."""


class GSageScheduledJobStatus(str, enum.Enum):
    """Last-execution status (informational)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    SKIPPED = "SKIPPED"


class GSageScheduledJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persistent definition of a user- or system-defined scheduled job."""

    __tablename__ = "gsage_scheduled_jobs"

    # ── Tenancy ────────────────────────────────────────────────────────────
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Owning organization (tenant isolation key).",
    )
    dept_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Department that owns this job. NULL = org-wide job.",
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Owning user — PROMPT_RUN jobs execute with this user's permissions.",
    )

    # ── Identity ───────────────────────────────────────────────────────────
    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Human-readable name for the job.",
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Optional longer description / notes.",
    )

    job_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="PROMPT_RUN | SYSTEM_TASK",
    )

    # ── Schedule ───────────────────────────────────────────────────────────
    cron_expression: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Standard 5-field crontab expression, e.g. '*/15 * * * *'.",
    )

    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        server_default="UTC",
        comment="IANA timezone name for cron evaluation, e.g. 'America/Sao_Paulo'.",
    )

    # ── Time window ────────────────────────────────────────────────────────
    starts_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Job is skipped if now < starts_at. NULL means always.",
    )

    ends_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Job self-deactivates after ends_at. NULL means indefinite.",
    )

    # ── State ──────────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        comment="Manual kill-switch. Set False to pause without deleting.",
    )

    max_runs: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Auto-deactivate after this many successful executions. NULL = unlimited.",
    )

    run_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="Total successful executions so far.",
    )

    # ── PROMPT_RUN fields ──────────────────────────────────────────────────
    prompt_content: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="[PROMPT_RUN] The prompt text sent to the agent.",
    )

    prompt_conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="[PROMPT_RUN] Target conversation UUID. NULL = create a fresh conversation each run.",
    )

    prompt_output_format: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="markdown",
        comment="[PROMPT_RUN] Output format: 'markdown' | 'plain'.",
    )

    # ── SYSTEM_TASK fields ─────────────────────────────────────────────────
    task_name: Mapped[Optional[str]] = mapped_column(
        String(300),
        nullable=True,
        comment="[SYSTEM_TASK] Fully-qualified Celery task name.",
    )

    task_kwargs: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="[SYSTEM_TASK] JSON kwargs passed to the Celery task.",
    )

    # ── Execution tracking ─────────────────────────────────────────────────
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp of the most recent execution attempt.",
    )

    last_run_status: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        comment="PENDING | RUNNING | SUCCESS | FAILURE | SKIPPED",
    )

    last_run_result: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Result payload from the last execution (truncated if large).",
    )

    # ── RedBeat integration ────────────────────────────────────────────────
    redbeat_key: Mapped[Optional[str]] = mapped_column(
        String(400),
        nullable=True,
        unique=True,
        comment="RedBeat Redis key for this entry — set on sync, cleared on remove.",
    )

    # ── Relationships ──────────────────────────────────────────────────────
    organization: Mapped["GSageOrganization"] = relationship(
        "GSageOrganization",
        back_populates=None,
        foreign_keys=[org_id],
        lazy="noload",
    )

    user: Mapped["GSageUser"] = relationship(
        "GSageUser",
        back_populates=None,
        foreign_keys=[user_id],
        lazy="noload",
    )
