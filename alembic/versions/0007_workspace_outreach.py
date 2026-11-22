"""Add per-workspace outreach inputs."""
from alembic import op
import sqlalchemy as sa

revision = "0007_workspace_outreach"
down_revision = "0006_workspace_schedule"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workspaces", sa.Column("outreach_candidate_id", sa.Integer(), nullable=True))
    op.add_column("workspaces", sa.Column("outreach_query", sa.String(length=500), nullable=False, server_default=""))
    op.add_column("workspaces", sa.Column("outreach_target_role", sa.String(length=300), nullable=False, server_default=""))


def downgrade():
    op.drop_column("workspaces", "outreach_target_role")
    op.drop_column("workspaces", "outreach_query")
    op.drop_column("workspaces", "outreach_candidate_id")
