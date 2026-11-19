"""001_initial_schema

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-11-19 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Long Term Memories table
    op.create_table(
        'long_term_memories',
        sa.Column('id', sa.String(length=255), primary_key=True),
        sa.Column('category', sa.String(length=64), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('idx_mem_category', 'long_term_memories', ['category'])
    op.create_index('idx_mem_created_at', 'long_term_memories', ['created_at'])

    # 2. Checkpoints table (LangGraph Persistence compatibility)
    op.create_table(
        'checkpoints',
        sa.Column('thread_id', sa.String(length=255), nullable=False),
        sa.Column('checkpoint_ns', sa.String(length=255), server_default='', nullable=False),
        sa.Column('checkpoint_id', sa.String(length=255), nullable=False),
        sa.Column('parent_checkpoint_id', sa.String(length=255), nullable=True),
        sa.Column('type', sa.String(length=64), nullable=True),
        sa.Column('checkpoint', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint('thread_id', 'checkpoint_ns', 'checkpoint_id')
    )
    op.create_index('idx_checkpoint_thread', 'checkpoints', ['thread_id'])

    # 3. Checkpoint Blobs table
    op.create_table(
        'checkpoint_blobs',
        sa.Column('thread_id', sa.String(length=255), nullable=False),
        sa.Column('checkpoint_ns', sa.String(length=255), server_default='', nullable=False),
        sa.Column('channel', sa.String(length=255), nullable=False),
        sa.Column('version', sa.String(length=255), nullable=False),
        sa.Column('type', sa.String(length=64), nullable=True),
        sa.Column('blob', sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint('thread_id', 'checkpoint_ns', 'channel', 'version')
    )

    # 4. Checkpoint Writes table
    op.create_table(
        'checkpoint_writes',
        sa.Column('thread_id', sa.String(length=255), nullable=False),
        sa.Column('checkpoint_ns', sa.String(length=255), server_default='', nullable=False),
        sa.Column('checkpoint_id', sa.String(length=255), nullable=False),
        sa.Column('task_id', sa.String(length=255), nullable=False),
        sa.Column('idx', sa.Integer(), nullable=False),
        sa.Column('channel', sa.String(length=255), nullable=False),
        sa.Column('type', sa.String(length=64), nullable=True),
        sa.Column('blob', sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint('thread_id', 'checkpoint_ns', 'checkpoint_id', 'task_id', 'idx')
    )


def downgrade() -> None:
    op.drop_table('checkpoint_writes')
    op.drop_table('checkpoint_blobs')
    op.drop_table('checkpoints')
    op.drop_index('idx_mem_created_at', table_name='long_term_memories')
    op.drop_index('idx_mem_category', table_name='long_term_memories')
    op.drop_table('long_term_memories')
