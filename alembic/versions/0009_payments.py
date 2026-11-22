"""Add payment records.

Revision ID: 0009_payments
Revises: 0008_jobs_system_settings
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_payments"
down_revision = "0008_jobs_system_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id"), nullable=True),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="succeeded"),
        sa.Column("amount_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="gbp"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider_payment_id"),
    )
    op.create_index("ix_payments_workspace_id", "payments", ["workspace_id"])
    op.create_index("ix_payments_invoice_id", "payments", ["invoice_id"])


def downgrade():
    op.drop_index("ix_payments_invoice_id", table_name="payments")
    op.drop_index("ix_payments_workspace_id", table_name="payments")
    op.drop_table("payments")
