"""Persist per-workspace outreach policy and automatic-send preference."""
from alembic import op
import sqlalchemy as sa

revision = "0011_workspace_outreach_policy"
down_revision = "0010_coupons"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("workspaces")}
    if "outreach_policy" not in columns:
        op.add_column("workspaces", sa.Column("outreach_policy", sa.String(length=30), nullable=True, server_default="review_required"))
    if "outreach_auto_send" not in columns:
        op.add_column("workspaces", sa.Column("outreach_auto_send", sa.Boolean(), nullable=True, server_default=sa.false()))
    op.execute(sa.text("UPDATE workspaces SET outreach_policy = 'review_required' WHERE outreach_policy IS NULL"))
    op.execute(sa.text("UPDATE workspaces SET outreach_auto_send = FALSE WHERE outreach_auto_send IS NULL"))
    op.alter_column("workspaces", "outreach_policy", nullable=False, server_default=None)
    op.alter_column("workspaces", "outreach_auto_send", nullable=False, server_default=None)


def downgrade():
    op.drop_column("workspaces", "outreach_auto_send")
    op.drop_column("workspaces", "outreach_policy")
