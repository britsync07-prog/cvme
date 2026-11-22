"""Add durable jobs and system settings.

Revision ID: 0008_jobs_system_settings
Revises: 0007_workspace_outreach
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_jobs_system_settings"
down_revision = "0007_workspace_outreach"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_system_settings_key", "system_settings", ["key"], unique=True)
    op.create_table(
        "outreach_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("job_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="queued"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_outreach_jobs_workspace_id", "outreach_jobs", ["workspace_id"])
    op.create_index("ix_outreach_jobs_user_id", "outreach_jobs", ["user_id"])
    op.create_index("ix_outreach_jobs_status", "outreach_jobs", ["status"])


def downgrade():
    op.drop_index("ix_outreach_jobs_status", table_name="outreach_jobs")
    op.drop_index("ix_outreach_jobs_user_id", table_name="outreach_jobs")
    op.drop_index("ix_outreach_jobs_workspace_id", table_name="outreach_jobs")
    op.drop_table("outreach_jobs")
    op.drop_index("ix_system_settings_key", table_name="system_settings")
    op.drop_table("system_settings")
