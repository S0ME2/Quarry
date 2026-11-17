"""Track the current selected evaluation for each campaign and tender."""

import sqlalchemy as sa
from alembic import op

revision = "0003_match_selections"
down_revision = "0002_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "match_selections",
        sa.Column("campaign_id", sa.Uuid(), sa.ForeignKey("campaigns.id"), primary_key=True),
        sa.Column("tender_id", sa.Uuid(), sa.ForeignKey("tenders.id"), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("match_jobs.id"), nullable=False),
        sa.Column("tender_version_id", sa.Uuid(), nullable=False),
        sa.Column("criteria_version_id", sa.Uuid(), nullable=False),
    )
    op.execute("""INSERT INTO match_selections
        SELECT DISTINCT ON (campaign_id, tender_id)
        campaign_id, tender_id, id, tender_version_id, criteria_version_id
        FROM match_jobs ORDER BY campaign_id, tender_id, created_at DESC, id DESC""")


def downgrade() -> None:
    op.drop_table("match_selections")
