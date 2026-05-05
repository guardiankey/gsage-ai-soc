"""add gsage_users.default_dept_id

Adds a nullable foreign key on gsage_users referencing gsage_departments.id
so each user can configure a default department to be opened on login.
ON DELETE SET NULL keeps the user record consistent if the department is
removed.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-05-05 09:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'gsage_users',
        sa.Column(
            'default_dept_id',
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment=(
                'Default department to open at login. NULL means the client '
                'auto-picks the first active department of the active org.'
            ),
        ),
    )
    op.create_index(
        'ix_gsage_users_default_dept_id',
        'gsage_users',
        ['default_dept_id'],
    )
    op.create_foreign_key(
        'fk_gsage_users_default_dept_id',
        'gsage_users',
        'gsage_departments',
        ['default_dept_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_gsage_users_default_dept_id', 'gsage_users', type_='foreignkey')
    op.drop_index('ix_gsage_users_default_dept_id', table_name='gsage_users')
    op.drop_column('gsage_users', 'default_dept_id')
