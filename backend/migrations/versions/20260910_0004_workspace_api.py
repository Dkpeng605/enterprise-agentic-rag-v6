"""Add anonymous sessions and workspace API fields.

Revision ID: 20260910_0004
Revises: 20260910_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_0004"
down_revision: str | None = "20260910_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("collections", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "collections",
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    )
    op.add_column(
        "collections",
        sa.Column("is_seed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        "ck_collections_status", "collections", "status IN ('active', 'deleting')"
    )
    op.add_column("documents", sa.Column("organization", sa.String(200), nullable=True))
    op.create_table(
        "anonymous_sessions",
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
            "length(token_hash) = 64", name="ck_anonymous_sessions_token_hash_length"
        ),
        sa.CheckConstraint("length(csrf_hash) = 64", name="ck_anonymous_sessions_csrf_hash_length"),
    )
    op.create_index("ix_anonymous_sessions_expires_at", "anonymous_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_anonymous_sessions_expires_at", table_name="anonymous_sessions")
    op.drop_table("anonymous_sessions")
    op.drop_column("documents", "organization")
    op.drop_constraint("ck_collections_status", "collections", type_="check")
    op.drop_column("collections", "is_seed")
    op.drop_column("collections", "status")
    op.drop_column("collections", "description")
