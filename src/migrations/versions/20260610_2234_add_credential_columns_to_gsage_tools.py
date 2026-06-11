"""add_credential_columns_to_gsage_tools

Revision ID: e55ab717bfe5
Revises: 44b5e8cfb02b
Create Date: 2026-06-10 22:34:41.117124

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e55ab717bfe5'
down_revision: Union[str, None] = '44b5e8cfb02b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Boolean column needs a server_default so existing rows get a value
    # without violating NOT NULL.  Default is dropped afterwards so the
    # application is responsible for setting it on every insert.
    op.add_column(
        'gsage_tools',
        sa.Column(
            'requires_user_credentials',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment='Tool requires a personal per-user credential',
        ),
    )
    op.alter_column('gsage_tools', 'requires_user_credentials', server_default=None)
    op.add_column(
        'gsage_tools',
        sa.Column(
            'credential_namespace',
            sa.String(length=100),
            nullable=True,
            comment='Shared namespace key for credential lookup; tool name used when NULL',
        ),
    )
    op.add_column(
        'gsage_tools',
        sa.Column(
            'credential_schema',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment='JSON schema describing the required credential fields',
        ),
    )


def downgrade() -> None:
    op.drop_column('gsage_tools', 'credential_schema')
    op.drop_column('gsage_tools', 'credential_namespace')
    op.drop_column('gsage_tools', 'requires_user_credentials')
