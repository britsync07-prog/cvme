"""Add per-workspace schedule controls."""
from alembic import op
import sqlalchemy as sa

revision = "0006_workspace_schedule"
down_revision = "0005_security_fields"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workspaces", sa.Column("timezone", sa.String(length=80), nullable=False, server_default="Asia/Dhaka"))
    op.add_column("workspaces", sa.Column("schedule_time", sa.String(length=5), nullable=False, server_default="09:00"))
    op.add_column("workspaces", sa.Column("daily_limit", sa.Integer(), nullable=False, server_default="100"))
    op.add_column("workspaces", sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("workspaces", "paused")
    op.drop_column("workspaces", "daily_limit")
    op.drop_column("workspaces", "schedule_time")
    op.drop_column("workspaces", "timezone")
