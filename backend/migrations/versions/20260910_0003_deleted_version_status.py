"""Allow document-version tombstones during deletion Saga.

Revision ID: 20260910_0003
Revises: 20260910_0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260910_0003"
down_revision: str | None = "20260910_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_document_versions_status", "document_versions", type_="check")
    op.create_check_constraint(
        "ck_document_versions_status",
        "document_versions",
        "status IN ('pending', 'processing', 'indexed', 'failed', 'superseded', 'deleted')",
    )


def downgrade() -> None:
    op.execute("UPDATE document_versions SET status = 'superseded' WHERE status = 'deleted'")
    op.drop_constraint("ck_document_versions_status", "document_versions", type_="check")
    op.create_check_constraint(
        "ck_document_versions_status",
        "document_versions",
        "status IN ('pending', 'processing', 'indexed', 'failed', 'superseded')",
    )
