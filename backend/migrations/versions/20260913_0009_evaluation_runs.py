"""Persist tenant-scoped evaluation runs and reports.

Revision ID: 20260913_0009
Revises: 20260913_0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260913_0009"
down_revision: str | None = "20260913_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("dataset_revision", sa.String(100), nullable=True),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("provider_profile", sa.String(100), nullable=True),
        sa.Column("provider", sa.String(100), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("prompt_revision", sa.String(100), nullable=True),
        sa.Column("index_revision", sa.String(100), nullable=True),
        sa.Column("commit_sha", sa.String(100), nullable=True),
        sa.Column("max_cases", sa.Integer(), nullable=False),
        sa.Column("max_llm_calls", sa.Integer(), nullable=False),
        sa.Column("estimated_llm_calls", sa.Integer(), nullable=False),
        sa.Column("completed_cases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("case_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_evaluation_runs_status",
        ),
        sa.CheckConstraint(
            "mode IN ('all', 'standard', 'deep')", name="ck_evaluation_runs_mode"
        ),
        sa.CheckConstraint("max_cases > 0", name="ck_evaluation_runs_max_cases_positive"),
        sa.CheckConstraint(
            "max_llm_calls >= 0", name="ck_evaluation_runs_max_llm_calls_non_negative"
        ),
        sa.CheckConstraint(
            "estimated_llm_calls >= 0",
            name="ck_evaluation_runs_estimated_llm_calls_non_negative",
        ),
        sa.CheckConstraint(
            "completed_cases >= 0", name="ck_evaluation_runs_completed_cases_non_negative"
        ),
        sa.CheckConstraint("total_cases > 0", name="ck_evaluation_runs_total_cases_positive"),
        sa.CheckConstraint(
            "completed_cases <= total_cases", name="ck_evaluation_runs_progress_within_total"
        ),
    )
    op.create_index(
        "ix_evaluation_runs_tenant_created",
        "evaluation_runs",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "uq_evaluation_runs_one_active_per_tenant",
        "evaluation_runs",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_evaluation_runs_one_active_per_tenant", table_name="evaluation_runs"
    )
    op.drop_index("ix_evaluation_runs_tenant_created", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
