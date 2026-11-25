"""Add immutable audit_logs table for HITL and data-lifecycle compliance.

Revision ID: 003_audit_logs
Revises: 002_checkpoint_retention
Create Date: 2026-11-25 15:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003_audit_logs"
down_revision: str | None = "002_checkpoint_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create append-only audit_logs table with integrity hash and indexes."""
    from src.agentic.memory.migrations import safe_alembic_inspect

    bind = op.get_bind() if hasattr(op, "get_bind") else None
    inspector = safe_alembic_inspect(sa, bind, offline_returns_none=True) if bind is not None else None
    table_exists = bool(inspector is not None and inspector.has_table("audit_logs"))
    if not table_exists:
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("actor_id", sa.String(length=255), nullable=False),
            sa.Column("thread_id", sa.String(length=255), nullable=True),
            sa.Column("step_id", sa.String(length=128), nullable=True),
            sa.Column("trace_id", sa.String(length=128), nullable=True),
            sa.Column("decision", sa.String(length=64), nullable=True),
            sa.Column("risk_level", sa.String(length=32), nullable=True),
            sa.Column(
                "details",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column("integrity_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
    indexes = (
        {idx["name"] for idx in inspector.get_indexes("audit_logs")}
        if inspector is not None and inspector.has_table("audit_logs")
        else set()
    )
    if "idx_audit_logs_created_at" not in indexes:
        op.create_index("idx_audit_logs_created_at", "audit_logs", ["created_at"])
    if "idx_audit_logs_event_type" not in indexes:
        op.create_index("idx_audit_logs_event_type", "audit_logs", ["event_type"])
    if "idx_audit_logs_actor_id" not in indexes:
        op.create_index("idx_audit_logs_actor_id", "audit_logs", ["actor_id"])

    if bind is not None and bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION prevent_audit_logs_mutation()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'audit_logs is append-only and immutable';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs;
            CREATE TRIGGER trg_audit_logs_immutable
            BEFORE UPDATE OR DELETE ON audit_logs
            FOR EACH ROW
            EXECUTE FUNCTION prevent_audit_logs_mutation();
            """
        )
        op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM PUBLIC;")


def downgrade() -> None:
    """Drop audit_logs table and indexes."""
    bind = op.get_bind() if hasattr(op, "get_bind") else None
    if bind is not None and bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs;")
        op.execute("DROP FUNCTION IF EXISTS prevent_audit_logs_mutation();")
    op.drop_index("idx_audit_logs_actor_id", table_name="audit_logs")
    op.drop_index("idx_audit_logs_event_type", table_name="audit_logs")
    op.drop_index("idx_audit_logs_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
