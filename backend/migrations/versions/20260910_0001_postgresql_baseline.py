"""Create the initial document-lifecycle PostgreSQL schema.

Revision ID: 20260910_0001
Revises: None
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamp_columns() -> tuple[sa.Column[datetime], sa.Column[datetime]]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        *timestamp_columns(),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="ck_tenants_status"),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        *timestamp_columns(),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_users_status"),
    )
    op.create_index("uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True)
    op.create_table(
        "memberships",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "role IN ('tenant_admin', 'editor', 'viewer')",
            name="ck_memberships_role",
        ),
    )
    op.create_table(
        "collections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False, server_default="tenant"),
        *timestamp_columns(),
        sa.CheckConstraint(
            "visibility IN ('private', 'tenant', 'public')",
            name="ck_collections_visibility",
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_collections_tenant_name"),
    )
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "collection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("logical_name", sa.String(300), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("active_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("visibility", sa.String(20), nullable=False, server_default="tenant"),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        *timestamp_columns(),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed', 'deleting', 'deleted')",
            name="ck_documents_status",
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'tenant', 'public')",
            name="ck_documents_visibility",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "collection_id",
            "logical_name",
            name="uq_documents_collection_logical_name",
        ),
    )
    op.create_index("ix_documents_tenant_status", "documents", ["tenant_id", "status"])
    op.create_table(
        "document_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("source_name", sa.String(500), nullable=False),
        sa.Column("media_type", sa.String(200), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("object_key", sa.String(500), nullable=False),
        sa.Column("parser_provider", sa.String(100), nullable=False),
        sa.Column("parser_version", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        *timestamp_columns(),
        sa.CheckConstraint("size_bytes >= 0", name="ck_document_versions_size_non_negative"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'indexed', 'failed', 'superseded')",
            name="ck_document_versions_status",
        ),
        sa.UniqueConstraint(
            "document_id",
            "sha256",
            name="uq_document_versions_document_sha",
        ),
    )
    op.create_foreign_key(
        "fk_documents_active_version_id_document_versions",
        "documents",
        "document_versions",
        ["active_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "roots",
        sa.Column("id", sa.String(69), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("index_revision", sa.String(100), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("source_locator", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("clean_text", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_roots_ordinal_non_negative"),
        sa.UniqueConstraint("version_id", "ordinal", name="uq_roots_version_ordinal"),
    )
    op.create_table(
        "leaves",
        sa.Column("id", sa.String(69), primary_key=True),
        sa.Column(
            "root_id",
            sa.String(69),
            sa.ForeignKey("roots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("retrieval_text", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_leaves_ordinal_non_negative"),
        sa.CheckConstraint("token_count > 0", name="ck_leaves_token_count_positive"),
        sa.UniqueConstraint("root_id", "ordinal", name="uq_leaves_root_ordinal"),
    )
    op.create_index("ix_leaves_version_id", "leaves", ["version_id"])
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(50), nullable=False, server_default="ingest"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(200), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage", sa.String(50), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        *timestamp_columns(),
        sa.CheckConstraint("attempts >= 0", name="ck_ingestion_jobs_attempts_non_negative"),
        sa.CheckConstraint("max_attempts > 0", name="ck_ingestion_jobs_max_attempts_positive"),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="ck_ingestion_jobs_progress_range"),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'retry_wait', 'succeeded', "
            "'failed', 'cancelled')",
            name="ck_ingestion_jobs_status",
        ),
    )
    op.create_index(
        "ix_ingestion_jobs_status_available",
        "ingestion_jobs",
        ["status", "available_at"],
    )
    op.create_table(
        "index_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="building"),
        *timestamp_columns(),
        sa.CheckConstraint("dimension > 0", name="ck_index_revisions_dimension_positive"),
        sa.CheckConstraint(
            "status IN ('building', 'active', 'retired', 'failed')",
            name="ck_index_revisions_status",
        ),
    )
    op.create_index(
        "uq_index_revisions_active_kind",
        "index_revisions",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_index_revisions_active_kind", table_name="index_revisions")
    op.drop_table("index_revisions")
    op.drop_index("ix_ingestion_jobs_status_available", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
    op.drop_index("ix_leaves_version_id", table_name="leaves")
    op.drop_table("leaves")
    op.drop_table("roots")
    op.drop_constraint(
        "fk_documents_active_version_id_document_versions",
        "documents",
        type_="foreignkey",
    )
    op.drop_table("document_versions")
    op.drop_index("ix_documents_tenant_status", table_name="documents")
    op.drop_table("documents")
    op.drop_table("collections")
    op.drop_table("memberships")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("tenants")
