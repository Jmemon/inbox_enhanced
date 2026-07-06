"""task engine: tasks, task_thread_links, task_state_entities, task_events

Revision ID: 0007_tasks
Revises: 0006_data_floor
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007_tasks"
down_revision: Union[str, Sequence[str], None] = "0006_data_floor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- tasks ---
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        # null user_id reserved for Phase-4 default classify-tasks; always set in 2A.
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="tracker"),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False, server_default=""),
        sa.Column("criteria", sa.Text(), nullable=False, server_default=""),
        sa.Column("state_schema", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tasks_user_id", "tasks", ["user_id"])

    # --- task_thread_links ---
    op.create_table(
        "task_thread_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("inbox_threads.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("origin", sa.String(8), nullable=False),  # 'llm' | 'user' — user rows are sticky
        sa.Column("state", sa.String(12), nullable=False, server_default="attached"),
        sa.Column("confidence", sa.Integer(), nullable=True),  # 0-100 at link time (llm origin)
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "thread_id", name="uq_task_thread"),
    )
    op.create_index("ix_task_thread_links_task_id", "task_thread_links", ["task_id"])
    op.create_index("ix_task_thread_links_thread_id", "task_thread_links", ["thread_id"])
    op.create_index("ix_task_thread_links_user_id", "task_thread_links", ["user_id"])

    # --- task_state_entities ---
    op.create_table(
        "task_state_entities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("entity_key", sa.String(255), nullable=False),  # normalized ('stripe'); '_self' for singleton
        sa.Column("display_name", sa.String(255), nullable=False),
        # ALWAYS derivable as a fold over applied task_events; refold_entity()
        # rebuilds this after a revert/reject.
        sa.Column("state", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "entity_key", name="uq_task_entity"),
    )
    op.create_index("ix_task_state_entities_task_id", "task_state_entities", ["task_id"])
    op.create_index("ix_task_state_entities_user_id", "task_state_entities", ["user_id"])

    # --- task_events ---
    op.create_table(
        "task_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=True),      # soft ptr — events outlive merges
        sa.Column("thread_id", sa.String(36), nullable=True),      # soft ptr (provenance)
        sa.Column("message_id", sa.String(36), nullable=True),     # soft ptr; null for user edits
        sa.Column("gmail_message_id", sa.String(64), nullable=True),  # denormalized — audit survives churn
        sa.Column("field", sa.String(64), nullable=True),          # 'stage' or attribute key
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("evidence_quote", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),      # 0-100
        sa.Column("origin", sa.String(8), nullable=False),         # 'llm' | 'user'
        sa.Column("status", sa.String(16), nullable=False),        # applied|pending_review|rejected|reverted
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.create_index("ix_task_events_user_id", "task_events", ["user_id"])
    op.create_index("ix_task_events_entity_id", "task_events", ["entity_id"])
    op.create_index("ix_task_events_created_at", "task_events", ["created_at"])

    # Enforces at-most-one applied/pending change per (task, message, field)
    # for LLM-sourced events; user edits (message_id IS NULL) are exempt.
    # Raw SQL: valid on both SQLite and Postgres, no dialect guard needed.
    op.execute("""
        CREATE UNIQUE INDEX uq_task_event_msg_field ON task_events (task_id, message_id, field)
        WHERE message_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_task_event_msg_field")

    op.drop_index("ix_task_events_created_at", table_name="task_events")
    op.drop_index("ix_task_events_entity_id", table_name="task_events")
    op.drop_index("ix_task_events_user_id", table_name="task_events")
    op.drop_index("ix_task_events_task_id", table_name="task_events")
    op.drop_table("task_events")

    op.drop_index("ix_task_state_entities_user_id", table_name="task_state_entities")
    op.drop_index("ix_task_state_entities_task_id", table_name="task_state_entities")
    op.drop_table("task_state_entities")

    op.drop_index("ix_task_thread_links_user_id", table_name="task_thread_links")
    op.drop_index("ix_task_thread_links_thread_id", table_name="task_thread_links")
    op.drop_index("ix_task_thread_links_task_id", table_name="task_thread_links")
    op.drop_table("task_thread_links")

    op.drop_index("ix_tasks_user_id", table_name="tasks")
    op.drop_table("tasks")
