"""bucket unification: fold buckets into tasks(kind='bucket')

Revision ID: 0009_bucket_unification
Revises: 0008_pending_reason

ID-preserving data copy: every bucket row becomes a task row with the SAME
id (inbox_threads.bucket_id values are never rewritten). state_schema is
NULL (classify-only); status='active'; version=1; criteria/is_deleted carry
over verbatim; created_at is set to now (buckets never tracked one).

FK retarget (inbox_threads.bucket_id -> tasks.id) is Postgres-only: the
0002_inbox migration's FK was created unnamed, so Postgres autonamed it
inbox_threads_bucket_id_fkey. SQLite doesn't enforce FKs at all (the test
suite's Base.metadata.create_all is the source of truth there) and ALTER
TABLE ... DROP CONSTRAINT isn't supported without a full table rebuild, which
isn't worth the risk for a constraint SQLite was never enforcing anyway.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_bucket_unification"
down_revision: Union[str, Sequence[str], None] = "0008_pending_reason"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK_NAME = "inbox_threads_bucket_id_fkey"


def upgrade() -> None:
    bind = op.get_bind()

    # Data copy: buckets -> tasks(kind='bucket'), same ids.
    op.execute("""
        INSERT INTO tasks (id, user_id, kind, name, goal, criteria, state_schema,
                           status, version, is_deleted, created_at)
        SELECT id, user_id, 'bucket', name, '', criteria, NULL,
               'active', 1, is_deleted, CURRENT_TIMESTAMP
        FROM buckets
    """)

    if bind.dialect.name == "postgresql":
        op.drop_constraint(_FK_NAME, "inbox_threads", type_="foreignkey")
        op.create_foreign_key(_FK_NAME, "inbox_threads", "tasks", ["bucket_id"], ["id"])
    # SQLite: no FK enforcement to retarget; skip.

    op.drop_table("buckets")


def downgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "buckets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("criteria", sa.Text(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_buckets_user_id"), "buckets", ["user_id"], unique=False)

    op.execute("""
        INSERT INTO buckets (id, user_id, name, criteria, is_deleted)
        SELECT id, user_id, name, criteria, is_deleted
        FROM tasks
        WHERE kind = 'bucket'
    """)

    if bind.dialect.name == "postgresql":
        op.drop_constraint(_FK_NAME, "inbox_threads", type_="foreignkey")
        op.create_foreign_key(_FK_NAME, "inbox_threads", "buckets", ["bucket_id"], ["id"])

    op.execute("DELETE FROM tasks WHERE kind = 'bucket'")
