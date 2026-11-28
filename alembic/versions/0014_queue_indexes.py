"""Add indexes for paged application queues and operational job lookups.

Revision ID: 0014_queue_indexes
Revises: 0013_job_search
"""
from alembic import op


revision = "0014_queue_indexes"
down_revision = "0013_job_search"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_campaigns_workspace_status_created_at",
        "campaigns",
        ["workspace_id", "status", "created_at"],
    )
    op.create_index(
        "ix_outreach_jobs_workspace_status_created_at",
        "outreach_jobs",
        ["workspace_id", "status", "created_at"],
    )


def downgrade():
    op.drop_index("ix_outreach_jobs_workspace_status_created_at", table_name="outreach_jobs")
    op.drop_index("ix_campaigns_workspace_status_created_at", table_name="campaigns")
