"""Integration test for migration 0007 (task engine tables).

Upgrades a fresh SQLite db to head and asserts the four new tables
(tasks, task_thread_links, task_state_entities, task_events) exist with
their key columns, that the partial unique index on task_events
(task_id, message_id, field WHERE message_id IS NOT NULL) is enforced,
and that downgrading to 0006_data_floor removes everything again.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _alembic_cfg(db_url: str) -> Config:
    os.environ["DATABASE_URL"] = db_url
    from app.config import get_settings
    get_settings.cache_clear()
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


@pytest.fixture
def migrated_db(tmp_path):
    db_path = tmp_path / "mig.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    cfg = _alembic_cfg(db_url)
    command.upgrade(cfg, "head")
    eng = create_engine(db_url, future=True)
    yield eng, cfg
    eng.dispose()


def _cols(eng, table: str) -> set[str]:
    return {c["name"] for c in inspect(eng).get_columns(table)}


def test_new_tables_exist_after_upgrade(migrated_db):
    eng, _ = migrated_db
    assert {"id", "user_id", "kind", "name", "goal", "criteria", "state_schema",
            "status", "version", "is_deleted", "created_at"} <= _cols(eng, "tasks")
    assert {"id", "task_id", "thread_id", "user_id", "origin", "state",
            "confidence", "created_at", "updated_at"} <= _cols(eng, "task_thread_links")
    assert {"id", "task_id", "user_id", "entity_key", "display_name", "state",
            "updated_at"} <= _cols(eng, "task_state_entities")
    assert {"id", "task_id", "user_id", "entity_id", "thread_id", "message_id",
            "gmail_message_id", "field", "old_value", "new_value", "evidence_quote",
            "confidence", "origin", "status", "created_at"} <= _cols(eng, "task_events")


def test_task_event_partial_unique_index(migrated_db):
    """Two NULL-message_id rows for the same (task_id, field) coexist, but a
    second non-NULL-message_id row with the same (task_id, message_id, field)
    triple raises IntegrityError."""
    eng, _ = migrated_db
    now = datetime.now(timezone.utc).isoformat()

    with eng.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email, created_at) VALUES (:id, :email, :created_at)"),
            {"id": "user-1", "email": "user1@example.com", "created_at": now},
        )
        conn.execute(
            text(
                "INSERT INTO tasks (id, user_id, kind, name, goal, criteria, status, "
                "version, is_deleted, created_at) VALUES (:id, :user_id, 'tracker', "
                "'Task 1', '', '', 'active', 1, 0, :created_at)"
            ),
            {"id": "task-1", "user_id": "user-1", "created_at": now},
        )

    # Two rows with NULL message_id and the same (task_id, field) coexist fine.
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO task_events (id, task_id, user_id, field, origin, status, created_at) "
                "VALUES (:id, :task_id, :user_id, :field, 'llm', 'applied', :created_at)"
            ),
            [
                {"id": "evt-1", "task_id": "task-1", "user_id": "user-1", "field": "stage", "created_at": now},
                {"id": "evt-2", "task_id": "task-1", "user_id": "user-1", "field": "stage", "created_at": now},
            ],
        )

    # First non-NULL message_id row for (task-1, msg-1, stage) succeeds.
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO task_events (id, task_id, user_id, message_id, field, origin, status, created_at) "
                "VALUES (:id, :task_id, :user_id, :message_id, :field, 'llm', 'applied', :created_at)"
            ),
            {"id": "evt-3", "task_id": "task-1", "user_id": "user-1",
             "message_id": "msg-1", "field": "stage", "created_at": now},
        )

    # A second insert with the identical (task_id, message_id, field) triple
    # violates the partial unique index.
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO task_events (id, task_id, user_id, message_id, field, origin, status, created_at) "
                    "VALUES (:id, :task_id, :user_id, :message_id, :field, 'llm', 'applied', :created_at)"
                ),
                {"id": "evt-4", "task_id": "task-1", "user_id": "user-1",
                 "message_id": "msg-1", "field": "stage", "created_at": now},
            )


def test_downgrade_removes_everything(migrated_db):
    eng, cfg = migrated_db
    command.downgrade(cfg, "0006_data_floor")
    assert not inspect(eng).has_table("task_events")
    assert not inspect(eng).has_table("task_state_entities")
    assert not inspect(eng).has_table("task_thread_links")
    assert not inspect(eng).has_table("tasks")
