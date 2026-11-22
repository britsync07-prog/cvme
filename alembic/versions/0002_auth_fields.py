"""Add authentication state fields."""
from alembic import op
import sqlalchemy as sa

revision = "0002_auth_fields"
down_revision = "0001_multitenancy"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    additions = {
        "terms_accepted_at": sa.DateTime(timezone=True),
        "last_login_at": sa.DateTime(timezone=True),
        "failed_login_count": sa.Integer(),
        "locked_until": sa.DateTime(timezone=True),
    }
    for name, column_type in additions.items():
        if name not in columns:
            op.add_column("users", sa.Column(name, column_type, nullable=True))


def downgrade():
    for name in ("locked_until", "failed_login_count", "last_login_at", "terms_accepted_at"):
        with op.batch_alter_table("users") as batch:
            batch.drop_column(name)
