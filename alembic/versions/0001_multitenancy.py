"""Add workspace identity and tenant foundation."""
from alembic import op
import sqlalchemy as sa
from app.db import Base
from app import models  # noqa: F401

revision = "0001_multitenancy"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installs get the complete schema from metadata; this migration is
    # intentionally additive so existing SQLite/Postgres data remains intact.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    # Create referenced identity tables before adding foreign keys to legacy rows.
    Base.metadata.create_all(bind=bind)
    # Existing application columns are added only when absent. The application
    # keeps them nullable during rollout; a later migration can enforce NOT NULL
    # after all historical rows have been assigned to a workspace.
    for table in ("candidates", "campaigns", "audit_events"):
        columns = {column["name"] for column in inspector.get_columns(table)} if table in tables else set()
        if "workspace_id" not in columns:
            op.add_column(table, sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=True))
        if "user_id" not in columns:
            op.add_column(table, sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
        if table != "audit_events" and "deleted_at" not in columns:
            op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    # Create all new tables with their complete model schema after the legacy
    # tables have received the additive ownership columns.
    Base.metadata.create_all(bind=bind)


def downgrade():
    for table in ("candidates", "campaigns", "audit_events"):
        for column in ("deleted_at", "user_id", "workspace_id"):
            with op.batch_alter_table(table) as batch:
                batch.drop_column(column)
    for table in ("notifications", "support_tickets", "usage_records", "audit_logs", "email_verification_tokens", "password_reset_tokens", "session_tokens", "ai_provider_configs", "email_accounts", "invoices", "subscriptions", "plans", "workspace_memberships", "workspaces", "users"):
        op.drop_table(table)
