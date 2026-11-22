"""Add billing fields and idempotent webhook events."""
from alembic import op
import sqlalchemy as sa

revision = "0003_billing_fields"
down_revision = "0002_auth_fields"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    plans = {c["name"] for c in inspector.get_columns("plans")}
    for name, typ in {
        "monthly_price_cents": sa.Integer(), "yearly_price_cents": sa.Integer(),
        "stripe_monthly_price_id": sa.String(255), "stripe_yearly_price_id": sa.String(255),
    }.items():
        if name not in plans:
            op.add_column("plans", sa.Column(name, typ, nullable=True))
    subscriptions = {c["name"] for c in inspector.get_columns("subscriptions")}
    for name, typ in {"current_period_end": sa.DateTime(timezone=True), "grace_ends_at": sa.DateTime(timezone=True)}.items():
        if name not in subscriptions:
            op.add_column("subscriptions", sa.Column(name, typ, nullable=True))
    if "billing_events" not in inspector.get_table_names():
        op.create_table(
            "billing_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("provider_event_id", sa.String(255), nullable=False, unique=True),
            sa.Column("event_type", sa.String(100), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_billing_events_provider_event_id", "billing_events", ["provider_event_id"], unique=True)


def downgrade():
    op.drop_index("ix_billing_events_provider_event_id", table_name="billing_events")
    op.drop_table("billing_events")
    for name in ("grace_ends_at", "current_period_end"):
        with op.batch_alter_table("subscriptions") as batch:
            batch.drop_column(name)
    for name in ("stripe_yearly_price_id", "stripe_monthly_price_id", "yearly_price_cents", "monthly_price_cents"):
        with op.batch_alter_table("plans") as batch:
            batch.drop_column(name)
