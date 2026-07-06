"""data floor: bodies, mirror flags, last_activity_at, llm_calls, FTS (pg only)

Revision ID: 0006_data_floor
Revises: 0005_newsletter_v2
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006_data_floor"
down_revision: Union[str, Sequence[str], None] = "0005_newsletter_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- inbox_messages ---
    op.add_column("inbox_messages", sa.Column("body_text", sa.Text(), nullable=True))
    op.add_column("inbox_messages", sa.Column(
        "labels", sa.JSON().with_variant(JSONB(), "postgresql"),
        nullable=False, server_default="[]"))
    op.add_column("inbox_messages", sa.Column(
        "is_unread", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("inbox_messages", sa.Column(
        "is_deleted", sa.Boolean(), nullable=False, server_default="false"))

    # --- inbox_threads ---
    op.add_column("inbox_threads", sa.Column(
        "is_archived", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("inbox_threads", sa.Column("last_activity_at", sa.BigInteger(), nullable=True))
    op.create_index("ix_inbox_threads_user_activity", "inbox_threads",
                    ["user_id", "last_activity_at"])
    # Backfill so pre-migration rows keep their sort position (list_threads
    # switches to ORDER BY last_activity_at).
    op.execute("""
        UPDATE inbox_threads SET last_activity_at = (
            SELECT MAX(inbox_messages.gmail_internal_date) FROM inbox_messages
            WHERE inbox_messages.thread_id = inbox_threads.id)
    """)

    # --- llm_calls ---
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("ttft_ms", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_llm_calls_user_id", "llm_calls", ["user_id"])
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])

    # --- FTS: Postgres only. SQLite (tests) skips this block; the search repo
    # has a non-PG fallback path so behavior stays testable. ---
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute("""
            ALTER TABLE inbox_messages ADD COLUMN search_tsv tsvector
            GENERATED ALWAYS AS (
                setweight(to_tsvector('english', coalesce(from_addr, '')), 'A') ||
                setweight(to_tsvector('english', left(coalesce(body_text, ''), 200000)), 'B')
            ) STORED
        """)
        op.execute("CREATE INDEX ix_inbox_messages_search_tsv ON inbox_messages USING GIN (search_tsv)")
        op.execute("""
            ALTER TABLE inbox_threads ADD COLUMN subject_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('english', coalesce(subject, ''))) STORED
        """)
        op.execute("CREATE INDEX ix_inbox_threads_subject_tsv ON inbox_threads USING GIN (subject_tsv)")
        op.execute("CREATE INDEX ix_inbox_messages_from_trgm ON inbox_messages USING GIN (from_addr gin_trgm_ops)")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_inbox_messages_from_trgm")
        op.execute("DROP INDEX IF EXISTS ix_inbox_threads_subject_tsv")
        op.execute("DROP INDEX IF EXISTS ix_inbox_messages_search_tsv")
        op.execute("ALTER TABLE inbox_threads DROP COLUMN IF EXISTS subject_tsv")
        op.execute("ALTER TABLE inbox_messages DROP COLUMN IF EXISTS search_tsv")

    op.drop_index("ix_llm_calls_created_at", table_name="llm_calls")
    op.drop_index("ix_llm_calls_user_id", table_name="llm_calls")
    op.drop_table("llm_calls")

    op.drop_index("ix_inbox_threads_user_activity", table_name="inbox_threads")
    op.drop_column("inbox_threads", "last_activity_at")
    op.drop_column("inbox_threads", "is_archived")

    op.drop_column("inbox_messages", "is_deleted")
    op.drop_column("inbox_messages", "is_unread")
    op.drop_column("inbox_messages", "labels")
    op.drop_column("inbox_messages", "body_text")
