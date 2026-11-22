"""Add tenant and global suppression records."""
from alembic import op
import sqlalchemy as sa

revision = "0004_suppressions"
down_revision = "0003_billing_fields"
branch_labels = None
depends_on = None


def upgrade():
    if "suppressions" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "suppressions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=True),
            sa.Column("email", sa.String(320), nullable=True),
            sa.Column("domain", sa.String(255), nullable=True),
            sa.Column("reason", sa.String(80), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_suppressions_workspace_id", "suppressions", ["workspace_id"])
        op.create_index("ix_suppressions_email", "suppressions", ["email"])
        op.create_index("ix_suppressions_domain", "suppressions", ["domain"])


def downgrade():
    op.drop_index("ix_suppressions_domain", table_name="suppressions")
    op.drop_index("ix_suppressions_email", table_name="suppressions")
    op.drop_index("ix_suppressions_workspace_id", table_name="suppressions")
    op.drop_table("suppressions")
