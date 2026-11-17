"""Add accounts without assigning legacy operator resources to public users."""

import sqlalchemy as sa
from alembic import op

revision = "0005_accounts"
down_revision = "0004_telegram_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failed_logins", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
    )
    op.create_table(
        "user_sessions",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])
    op.create_table(
        "account_campaigns",
        sa.Column("campaign_id", sa.Uuid(), sa.ForeignKey("campaigns.id"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("last_scheduled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("interval_seconds >= 60", name="ck_account_campaign_interval"),
    )
    op.create_index("ix_account_campaigns_user_id", "account_campaigns", ["user_id"])
    op.create_table(
        "account_sources",
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
    )
    op.create_index("ix_account_sources_user_id", "account_sources", ["user_id"])
    op.add_column("telegram_notifications", sa.Column("channel", sa.Text()))
    op.add_column("telegram_notifications", sa.Column("destination", sa.Text()))


def downgrade() -> None:
    op.drop_column("telegram_notifications", "destination")
    op.drop_column("telegram_notifications", "channel")
    for table in ("account_sources", "account_campaigns", "user_sessions", "users"):
        op.drop_table(table)
