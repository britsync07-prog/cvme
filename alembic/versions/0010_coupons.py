"""Add platform coupons.

Revision ID: 0010_coupons
Revises: 0009_payments
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_coupons"
down_revision = "0009_payments"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "coupons",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("provider_coupon_id", sa.String(length=255), nullable=True),
        sa.Column("percent_off", sa.Integer(), nullable=True),
        sa.Column("amount_off_cents", sa.Integer(), nullable=True),
        sa.Column("duration", sa.String(length=20), nullable=False, server_default="once"),
        sa.Column("max_redemptions", sa.Integer(), nullable=True),
        sa.Column("redeemed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_coupons_code", "coupons", ["code"], unique=True)


def downgrade():
    op.drop_index("ix_coupons_code", table_name="coupons")
    op.drop_table("coupons")
