"""Add concurrency-safe document content claims.

Revision ID: 20260910_0002
Revises: 20260910_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_0002"
down_revision: str | None = "20260910_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_document_versions_sha256_format",
        "document_versions",
        "sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_table(
        "document_content_claims",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_document_content_claims_sha256_format",
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["collections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "collection_id",
            "sha256",
            name="pk_document_content_claims",
        ),
        sa.UniqueConstraint("version_id", name="uq_document_content_claims_version"),
    )
    op.create_index(
        "ix_document_content_claims_document_id",
        "document_content_claims",
        ["document_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_content_claims_document_id",
        table_name="document_content_claims",
    )
    op.drop_table("document_content_claims")
    op.drop_constraint(
        "ck_document_versions_sha256_format",
        "document_versions",
        type_="check",
    )
