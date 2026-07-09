"""jobs: persisted, pollable progress rows for the creation wizard and
bucket-delete re-triage (Phase 4.5 jobs surface, spec 005)

Revision ID: 0010_jobs
Revises: 0009_bucket_unification
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010_jobs"
down_revision: Union[str, Sequence[str], None] = "0009_bucket_unification"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),  # 'creation' | 'delete_retriage'
        sa.Column("task_kind", sa.String(16), nullable=True),  # 'tracker' | 'bucket' — creation jobs only
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("needs_user", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("payload", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("goal", sa.Text(), nullable=False, server_default=""),
        sa.Column("scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_user_id", table_name="jobs")
    op.drop_table("jobs")
