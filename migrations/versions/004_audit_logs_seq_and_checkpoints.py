"""Add monotonic audit_logs chain_seq and pre-seed LangGraph PostgresSaver schema.

Revision ID: 004_audit_seq_checkpoints
Revises: 003_audit_logs
Create Date: 2026-11-27 04:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "004_audit_seq_checkpoints"
down_revision: str | None = "003_audit_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add audit_logs.chain_seq ordering and align LangGraph checkpoint schema."""
    from src.agentic.memory.migrations import safe_alembic_inspect

    bind = op.get_bind() if hasattr(op, "get_bind") else None
    inspector = safe_alembic_inspect(sa, bind) if bind is not None else None

    audit_cols = (
        {col["name"] for col in inspector.get_columns("audit_logs")}
        if inspector is not None and inspector.has_table("audit_logs")
        else set()
    )
    if "chain_seq" not in audit_cols:
        op.add_column(
            "audit_logs",
            sa.Column(
                "chain_seq",
                sa.BigInteger(),
                sa.Identity(always=False),
                nullable=False,
            ),
        )
    audit_indexes = (
        {idx["name"] for idx in inspector.get_indexes("audit_logs")}
        if inspector is not None and inspector.has_table("audit_logs")
        else set()
    )
    if "idx_audit_logs_chain_seq" not in audit_indexes:
        op.create_index("idx_audit_logs_chain_seq", "audit_logs", ["chain_seq"])

    writes_cols = (
        {col["name"] for col in inspector.get_columns("checkpoint_writes")}
        if inspector is not None and inspector.has_table("checkpoint_writes")
        else set()
    )
    if "task_path" not in writes_cols:
        op.add_column(
            "checkpoint_writes",
            sa.Column("task_path", sa.Text(), server_default="", nullable=False),
        )

    if inspector is None or not inspector.has_table("checkpoint_migrations"):
        op.create_table(
            "checkpoint_migrations",
            sa.Column("v", sa.Integer(), primary_key=True, autoincrement=False),
        )

    chk_indexes = (
        {idx["name"] for idx in inspector.get_indexes("checkpoints")}
        if inspector is not None and inspector.has_table("checkpoints")
        else set()
    )
    if "checkpoints_thread_id_idx" not in chk_indexes:
        op.create_index("checkpoints_thread_id_idx", "checkpoints", ["thread_id"])

    blob_indexes = (
        {idx["name"] for idx in inspector.get_indexes("checkpoint_blobs")}
        if inspector is not None and inspector.has_table("checkpoint_blobs")
        else set()
    )
    if "checkpoint_blobs_thread_id_idx" not in blob_indexes:
        op.create_index(
            "checkpoint_blobs_thread_id_idx", "checkpoint_blobs", ["thread_id"]
        )

    writes_indexes = (
        {idx["name"] for idx in inspector.get_indexes("checkpoint_writes")}
        if inspector is not None and inspector.has_table("checkpoint_writes")
        else set()
    )
    if "checkpoint_writes_thread_id_idx" not in writes_indexes:
        op.create_index(
            "checkpoint_writes_thread_id_idx", "checkpoint_writes", ["thread_id"]
        )

    if bind is not None and bind.dialect.name == "postgresql":
        op.execute(
            "INSERT INTO checkpoint_migrations (v) VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9) "
            "ON CONFLICT (v) DO NOTHING;"
        )


def downgrade() -> None:
    """Revert audit_logs.chain_seq and LangGraph v9 checkpoint alignment."""
    op.drop_index("checkpoint_writes_thread_id_idx", table_name="checkpoint_writes")
    op.drop_index("checkpoint_blobs_thread_id_idx", table_name="checkpoint_blobs")
    op.drop_index("checkpoints_thread_id_idx", table_name="checkpoints")
    op.drop_table("checkpoint_migrations")
    op.drop_column("checkpoint_writes", "task_path")
    op.drop_index("idx_audit_logs_chain_seq", table_name="audit_logs")
    op.drop_column("audit_logs", "chain_seq")
