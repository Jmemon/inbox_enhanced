"""task_events: pending_reason + proposed_entity (pending provenance)

Revision ID: 0008_pending_reason
Revises: 0007_tasks
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008_pending_reason"
down_revision: Union[str, Sequence[str], None] = "0007_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Two nullable columns — no backfill needed (every existing row predates
    # the concept and is simply un-annotated); no dialect guards, both are
    # plain String columns on both SQLite and Postgres.
    op.add_column("task_events", sa.Column("pending_reason", sa.String(32), nullable=True))
    op.add_column("task_events", sa.Column("proposed_entity", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("task_events", "proposed_entity")
    op.drop_column("task_events", "pending_reason")
