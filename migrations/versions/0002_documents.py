"""Store independently processed tender attachments."""

import sqlalchemy as sa
from alembic import op

revision = "0002_documents"
down_revision = "0001_quarry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tender_documents",
        sa.Column("tender_id", sa.Uuid(), sa.ForeignKey("tenders.id"), primary_key=True),
        sa.Column("url", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("etag", sa.Text()),
        sa.Column("last_modified", sa.Text()),
        sa.Column("content_hash", sa.Text()),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tender_documents")
