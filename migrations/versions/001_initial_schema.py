"""001_initial_schema

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-11-19 03:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001_initial_schema'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from src.agentic.memory.migrations import safe_alembic_inspect

    bind = op.get_bind()
    inspector = safe_alembic_inspect(sa, bind)

    # 1. Long Term Memories table
    if not inspector.has_table('long_term_memories'):
        op.create_table(
            'long_term_memories',
            sa.Column('id', sa.String(length=255), primary_key=True),
            sa.Column('category', sa.String(length=64), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    mem_indexes = {idx['name'] for idx in inspector.get_indexes('long_term_memories')} if inspector.has_table('long_term_memories') else set()
    if 'idx_mem_category' not in mem_indexes:
        op.create_index('idx_mem_category', 'long_term_memories', ['category'])
    if 'idx_mem_created_at' not in mem_indexes:
        op.create_index('idx_mem_created_at', 'long_term_memories', ['created_at'])

    # 2. Checkpoints table (LangGraph Persistence compatibility)
    if not inspector.has_table('checkpoints'):
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
    chk_indexes = {idx['name'] for idx in inspector.get_indexes('checkpoints')} if inspector.has_table('checkpoints') else set()
    if 'idx_checkpoint_thread' not in chk_indexes:
        op.create_index('idx_checkpoint_thread', 'checkpoints', ['thread_id'])

    # 3. Checkpoint Blobs table
    if not inspector.has_table('checkpoint_blobs'):
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
    if not inspector.has_table('checkpoint_writes'):
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
