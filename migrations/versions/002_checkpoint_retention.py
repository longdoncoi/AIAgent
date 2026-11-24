"""Add checkpoint retention timestamp.

Revision ID: 002_checkpoint_retention
Revises: 001_initial_schema
Create Date: 2026-11-24 22:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_checkpoint_retention"
down_revision: str | None = "001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Timestamp every checkpoint so stale threads can be purged safely."""
    op.add_column(
        "checkpoints",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("idx_checkpoints_created_at", "checkpoints", ["created_at"])


def downgrade() -> None:
    """Remove checkpoint retention metadata."""
    op.drop_index("idx_checkpoints_created_at", table_name="checkpoints")
    op.drop_column("checkpoints", "created_at")
