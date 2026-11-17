"""Durable Telegram delivery queue."""

import sqlalchemy as sa
from alembic import op

revision = "0004_telegram_notifications"
down_revision = "0003_match_selections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_notifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("tender_id", sa.Uuid(), sa.ForeignKey("tenders.id"), nullable=False),
        sa.Column("tender_version_id", sa.Uuid(), nullable=False),
        sa.Column("result_id", sa.Uuid(), sa.ForeignKey("match_results.id"), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "campaign_id", "tender_id", "tender_version_id", name="uq_telegram_match"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'uncertain', 'delivered', 'superseded')",
            name="ck_telegram_status",
        ),
    )
    op.create_index(
        "ix_telegram_pending",
        "telegram_notifications",
        ["available_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("telegram_notifications")
