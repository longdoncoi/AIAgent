"""Add the persistent task store required by the A2A v1 server.

Revision ID: 002_a2a_task_store
Revises: 001_initial_schema
Create Date: 2026-11-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_a2a_task_store"
down_revision: str | None = "001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("context_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="task"),
        sa.Column("owner", sa.String(length=255), nullable=True),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
        sa.Column("status", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=True),
        sa.Column("history", sa.JSON(), nullable=True),
        sa.Column("protocol_version", sa.String(length=16), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_a2a_tasks_id", "a2a_tasks", ["id"])
    op.create_index("idx_a2a_tasks_owner_last_updated", "a2a_tasks", ["owner", "last_updated"])


def downgrade() -> None:
    op.drop_index("idx_a2a_tasks_owner_last_updated", table_name="a2a_tasks")
    op.drop_index("ix_a2a_tasks_id", table_name="a2a_tasks")
    op.drop_table("a2a_tasks")
