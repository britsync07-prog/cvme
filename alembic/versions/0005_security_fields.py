"""Add OAuth and optional 2FA user fields."""
from alembic import op
import sqlalchemy as sa

revision = "0005_security_fields"
down_revision = "0004_suppressions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("google_sub", sa.String(length=255), nullable=True))
    op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)
    op.add_column("users", sa.Column("totp_secret", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret")
    op.drop_index("ix_users_google_sub", table_name="users")
    op.drop_column("users", "google_sub")
