"""gSage AI — Approval rule resolution service.

The single public function :func:`find_approver` queries
:class:`~src.shared.models.approval_rule.GSageApprovalRule` rows and
returns the ``approver_user_id`` of the most specific matching rule.

Specificity scoring
-------------------
Each pattern field that is NOT the wildcard ``"*"`` contributes **+2** to the
specificity score (maximum 8).  When two rules have equal specificity the one
with the higher ``priority`` value wins.  If multiple rows still tie (same
score **and** same priority) the first row returned by the DB is used — the
ordering is stable because Postgres returns them by PK within a tie.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.approval_rule import GSageApprovalRule

logger = logging.getLogger(__name__)

_WILDCARD = "*"


def _specificity(rule: GSageApprovalRule, org_id: str, user_id: str, dept_id: Optional[str], tool_name: str) -> int:
    """Return the specificity score (0-8) for *rule* against the concrete values."""
    score = 0
    if rule.org_id_pattern != _WILDCARD:
        score += 2
    if rule.dept_id_pattern != _WILDCARD:
        score += 2
    if rule.user_id_pattern != _WILDCARD:
        score += 2
    if rule.tool_pattern != _WILDCARD:
        score += 2
    return score


async def find_approver(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    tool_name: str,
    db: AsyncSession,
    dept_id: Optional[uuid.UUID] = None,
) -> Optional[uuid.UUID]:
    """Return the :attr:`approver_user_id` for the most specific active rule.

    Queries all active rules where every pattern field matches the concrete
    value or is the wildcard ``"*"``.  Picks the best match by (specificity
    DESC, priority DESC).  Returns ``None`` when no rule matches — meaning no
    delegation is created and the original requester remains the approver.

    Parameters
    ----------
    org_id:
        UUID of the tenant organisation.
    user_id:
        UUID of the user who triggered the tool call.
    tool_name:
        Name of the tool that requires approval.
    db:
        Active async SQLAlchemy session.
    dept_id:
        Optional UUID of the user's active department. When provided, rules
        are also matched against ``dept_id_pattern``.
    """
    org_str = str(org_id)
    user_str = str(user_id)
    dept_str = str(dept_id) if dept_id else None

    dept_filter = (
        or_(
            GSageApprovalRule.dept_id_pattern == dept_str,
            GSageApprovalRule.dept_id_pattern == _WILDCARD,
        )
        if dept_str
        else GSageApprovalRule.dept_id_pattern == _WILDCARD
    )

    stmt = (
        select(GSageApprovalRule)
        .where(
            and_(
                GSageApprovalRule.is_active == True,  # noqa: E712
                or_(
                    GSageApprovalRule.org_id_pattern == org_str,
                    GSageApprovalRule.org_id_pattern == _WILDCARD,
                ),
                dept_filter,
                or_(
                    GSageApprovalRule.user_id_pattern == user_str,
                    GSageApprovalRule.user_id_pattern == _WILDCARD,
                ),
                or_(
                    GSageApprovalRule.tool_pattern == tool_name,
                    GSageApprovalRule.tool_pattern == _WILDCARD,
                ),
            )
        )
    )

    result = await db.execute(stmt)
    candidates = result.scalars().all()

    if not candidates:
        return None

    best = max(
        candidates,
        key=lambda r: (
            _specificity(r, org_str, user_str, dept_str, tool_name),
            r.priority,
        ),
    )

    logger.debug(
        "find_approver: org=%s user=%s tool=%s → rule=%s approver=%s",
        org_str,
        user_str,
        tool_name,
        best.id,
        best.approver_user_id,
    )
    return best.approver_user_id
