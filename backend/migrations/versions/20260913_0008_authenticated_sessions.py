"""Add system roles and authenticated administrator sessions.

Revision ID: 20260913_0008
Revises: 20260912_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260913_0008"
down_revision: str | None = "20260912_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("system_role", sa.String(30), nullable=True))
    op.create_check_constraint(
        "ck_users_system_role",
        "users",
        "system_role IS NULL OR system_role IN ('super_admin', 'system_admin', 'auditor')",
    )
    op.create_table(
        "authenticated_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64", name="ck_authenticated_sessions_token_hash_length"
        ),
        sa.CheckConstraint(
            "length(csrf_hash) = 64", name="ck_authenticated_sessions_csrf_hash_length"
        ),
    )
    op.create_index(
        "ix_authenticated_sessions_expires_at", "authenticated_sessions", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_authenticated_sessions_expires_at", table_name="authenticated_sessions")
    op.drop_table("authenticated_sessions")
    op.drop_constraint("ck_users_system_role", "users", type_="check")
    op.drop_column("users", "system_role")
