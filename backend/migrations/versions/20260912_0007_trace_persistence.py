"""Add tenant-scoped trace runs and spans.

Revision ID: 20260912_0007
Revises: 20260912_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260912_0007"
down_revision: str | None = "20260912_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trace_runs",
        sa.Column("trace_id", sa.String(32), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_type", sa.String(30), nullable=False),
        sa.Column("trace_type", sa.String(20), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("mode", sa.String(30), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("usage", postgresql.JSONB(), nullable=False),
        sa.Column("attributes", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "trace_type IN ('query', 'ingestion', 'evaluation')",
            name="ck_trace_runs_trace_type",
        ),
        sa.CheckConstraint("finished_at >= started_at", name="ck_trace_runs_time_order"),
    )
    op.create_index(
        "ix_trace_runs_tenant_type_started",
        "trace_runs",
        ["tenant_id", "trace_type", "started_at"],
    )
    op.create_table(
        "trace_spans",
        sa.Column(
            "trace_id",
            sa.String(32),
            sa.ForeignKey("trace_runs.trace_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("span_id", sa.String(16), primary_key=True),
        sa.Column("parent_span_id", sa.String(16), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attributes", postgresql.JSONB(), nullable=False),
        sa.Column("events", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("finished_at >= started_at", name="ck_trace_spans_time_order"),
    )
    op.create_index(
        "ix_trace_spans_trace_started", "trace_spans", ["trace_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_trace_spans_trace_started", table_name="trace_spans")
    op.drop_table("trace_spans")
    op.drop_index("ix_trace_runs_tenant_type_started", table_name="trace_runs")
    op.drop_table("trace_runs")
