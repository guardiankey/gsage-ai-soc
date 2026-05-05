"""expand channel_chat_id and channel_message_id to 512

Teams conversation/user IDs exceed the previous VARCHAR(100) limit.
Widens channel_chat_id on both gsage_channel_conversations and
gsage_channel_messages, and channel_message_id on gsage_channel_messages,
from VARCHAR(100) to VARCHAR(512).

Revision ID: b1c2d3e4f5a6
Revises: 39fb895cd21b
Create Date: 2026-05-04 22:40:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = '39fb895cd21b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # gsage_channel_conversations.channel_chat_id
    op.alter_column(
        'gsage_channel_conversations',
        'channel_chat_id',
        existing_type=sa.String(length=100),
        type_=sa.String(length=512),
        existing_nullable=False,
    )

    # gsage_channel_messages.channel_chat_id
    op.alter_column(
        'gsage_channel_messages',
        'channel_chat_id',
        existing_type=sa.String(length=100),
        type_=sa.String(length=512),
        existing_nullable=False,
    )

    # gsage_channel_messages.channel_message_id
    op.alter_column(
        'gsage_channel_messages',
        'channel_message_id',
        existing_type=sa.String(length=100),
        type_=sa.String(length=512),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        'gsage_channel_messages',
        'channel_message_id',
        existing_type=sa.String(length=512),
        type_=sa.String(length=100),
        existing_nullable=False,
    )

    op.alter_column(
        'gsage_channel_messages',
        'channel_chat_id',
        existing_type=sa.String(length=512),
        type_=sa.String(length=100),
        existing_nullable=False,
    )

    op.alter_column(
        'gsage_channel_conversations',
        'channel_chat_id',
        existing_type=sa.String(length=512),
        type_=sa.String(length=100),
        existing_nullable=False,
    )
