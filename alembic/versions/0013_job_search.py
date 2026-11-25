"""Add tenant-scoped job search, saved jobs, contacts, and campaign linkage.

Revision ID: 0013_job_search
Revises: 0012_user_soft_delete
"""
from alembic import op
import sqlalchemy as sa


revision = "0013_job_search"
down_revision = "0012_user_soft_delete"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("source_job_id", sa.String(length=255), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("company_name", sa.String(length=300), nullable=False),
        sa.Column("company_website", sa.String(length=1000), nullable=True),
        sa.Column("location_text", sa.String(length=500), nullable=True),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("city", sa.String(length=160), nullable=True),
        sa.Column("is_remote", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("job_type", sa.String(length=80), nullable=True),
        sa.Column("category", sa.String(length=80), nullable=True),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("salary_currency", sa.String(length=8), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("apply_url", sa.String(length=2000), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.UniqueConstraint("workspace_id", "external_id", name="uq_job_workspace_external"),
    )
    for name, columns in (("ix_jobs_workspace_id", ["workspace_id"]), ("ix_jobs_external_id", ["external_id"]), ("ix_jobs_source", ["source"]), ("ix_jobs_company_name", ["company_name"]), ("ix_jobs_country_code", ["country_code"]), ("ix_jobs_posted_at", ["posted_at"]), ("ix_jobs_fetched_at", ["fetched_at"])):
        op.create_index(name, "jobs", columns)
    op.create_table(
        "job_searches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("query", sa.JSON(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_statuses", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name, columns in (("ix_job_searches_workspace_id", ["workspace_id"]), ("ix_job_searches_user_id", ["user_id"]), ("ix_job_searches_created_at", ["created_at"])):
        op.create_index(name, "job_searches", columns)
    op.create_table(
        "saved_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "job_id", name="uq_saved_job_workspace_job"),
    )
    for name, columns in (("ix_saved_jobs_workspace_id", ["workspace_id"]), ("ix_saved_jobs_user_id", ["user_id"]), ("ix_saved_jobs_job_id", ["job_id"])):
        op.create_index(name, "saved_jobs", columns)
    op.create_table(
        "company_contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("company_name", sa.String(length=300), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="generic"),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("found_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_mx", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("workspace_id", "job_id", "email", name="uq_company_contact_job_email"),
    )
    for name, columns in (("ix_company_contacts_workspace_id", ["workspace_id"]), ("ix_company_contacts_job_id", ["job_id"]), ("ix_company_contacts_domain", ["domain"]), ("ix_company_contacts_email", ["email"])):
        op.create_index(name, "company_contacts", columns)
    op.add_column("campaigns", sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True))
    op.create_index("ix_campaigns_job_id", "campaigns", ["job_id"])


def downgrade():
    op.drop_index("ix_campaigns_job_id", table_name="campaigns")
    op.drop_column("campaigns", "job_id")
    for name in ("ix_company_contacts_email", "ix_company_contacts_domain", "ix_company_contacts_job_id", "ix_company_contacts_workspace_id"):
        op.drop_index(name, table_name="company_contacts")
    op.drop_table("company_contacts")
    for name in ("ix_saved_jobs_job_id", "ix_saved_jobs_user_id", "ix_saved_jobs_workspace_id"):
        op.drop_index(name, table_name="saved_jobs")
    op.drop_table("saved_jobs")
    for name in ("ix_job_searches_created_at", "ix_job_searches_user_id", "ix_job_searches_workspace_id"):
        op.drop_index(name, table_name="job_searches")
    op.drop_table("job_searches")
    for name in ("ix_jobs_fetched_at", "ix_jobs_posted_at", "ix_jobs_country_code", "ix_jobs_company_name", "ix_jobs_source", "ix_jobs_external_id", "ix_jobs_workspace_id"):
        op.drop_index(name, table_name="jobs")
    op.drop_table("jobs")
