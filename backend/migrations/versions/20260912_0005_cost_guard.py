"""Add atomic usage windows and durable query budget reservations.

Revision ID: 20260912_0005
Revises: 20260910_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260912_0005"
down_revision: str | None = "20260910_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "query_usage_windows",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("window_kind", sa.String(10), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("query_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "window_kind IN ('minute', 'day')", name="ck_query_usage_windows_window_kind"
        ),
        sa.CheckConstraint(
            "query_count >= 0", name="ck_query_usage_windows_query_count_non_negative"
        ),
        sa.CheckConstraint("llm_calls >= 0", name="ck_query_usage_windows_llm_calls_non_negative"),
        sa.CheckConstraint(
            "input_tokens >= 0", name="ck_query_usage_windows_input_tokens_non_negative"
        ),
        sa.CheckConstraint(
            "output_tokens >= 0", name="ck_query_usage_windows_output_tokens_non_negative"
        ),
    )
    op.create_table(
        "query_budget_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("query_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("minute_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("day_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rate_limit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reserved_llm_calls", sa.Integer(), nullable=False),
        sa.Column("reserved_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("actual_llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actual_input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("actual_output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("settled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "reserved_llm_calls >= 0",
            name="ck_query_budget_reservations_reserved_llm_calls_non_negative",
        ),
        sa.CheckConstraint(
            "reserved_input_tokens >= 0",
            name="ck_query_budget_reservations_reserved_input_tokens_non_negative",
        ),
        sa.CheckConstraint(
            "reserved_output_tokens >= 0",
            name="ck_query_budget_reservations_reserved_output_tokens_non_negative",
        ),
        sa.CheckConstraint(
            "actual_llm_calls >= 0",
            name="ck_query_budget_reservations_actual_llm_calls_non_negative",
        ),
        sa.CheckConstraint(
            "actual_input_tokens >= 0",
            name="ck_query_budget_reservations_actual_input_tokens_non_negative",
        ),
        sa.CheckConstraint(
            "actual_output_tokens >= 0",
            name="ck_query_budget_reservations_actual_output_tokens_non_negative",
        ),
    )


def downgrade() -> None:
    op.drop_table("query_budget_reservations")
    op.drop_table("query_usage_windows")
