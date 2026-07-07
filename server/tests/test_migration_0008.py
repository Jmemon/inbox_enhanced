"""Integration test for migration 0008 (task_events.pending_reason +
proposed_entity — pending provenance for the review tray).

Upgrades a fresh SQLite db to head, asserts the two new nullable columns
exist and round-trip a row through each, and that downgrading to 0007_tasks
removes them again while leaving the table itself intact. Copies
test_migration_0007's pattern (ISO-string datetime binds via raw SQL).
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

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


def test_new_columns_exist_after_upgrade(migrated_db):
    eng, _ = migrated_db
    assert {"pending_reason", "proposed_entity"} <= _cols(eng, "task_events")


def test_columns_are_nullable_and_round_trip_values(migrated_db):
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
        # A row with no reason at all (e.g. an applied event) leaves both
        # columns NULL.
        conn.execute(
            text(
                "INSERT INTO task_events (id, task_id, user_id, field, origin, status, created_at) "
                "VALUES (:id, :task_id, :user_id, 'stage', 'llm', 'applied', :created_at)"
            ),
            {"id": "evt-1", "task_id": "task-1", "user_id": "user-1", "created_at": now},
        )
        # A near_duplicate_entity pending row carries both.
        conn.execute(
            text(
                "INSERT INTO task_events (id, task_id, user_id, field, origin, status, "
                "pending_reason, proposed_entity, created_at) VALUES "
                "(:id, :task_id, :user_id, 'stage', 'llm', 'pending_review', "
                ":pending_reason, :proposed_entity, :created_at)"
            ),
            {"id": "evt-2", "task_id": "task-1", "user_id": "user-1",
             "pending_reason": "near_duplicate_entity", "proposed_entity": "Stripewise Corp",
             "created_at": now},
        )

    with eng.connect() as conn:
        row1 = conn.execute(
            text("SELECT pending_reason, proposed_entity FROM task_events WHERE id = 'evt-1'")
        ).one()
        assert row1.pending_reason is None
        assert row1.proposed_entity is None

        row2 = conn.execute(
            text("SELECT pending_reason, proposed_entity FROM task_events WHERE id = 'evt-2'")
        ).one()
        assert row2.pending_reason == "near_duplicate_entity"
        assert row2.proposed_entity == "Stripewise Corp"


def test_downgrade_removes_columns_but_keeps_table(migrated_db):
    eng, cfg = migrated_db
    command.downgrade(cfg, "0007_tasks")
    assert inspect(eng).has_table("task_events")
    assert not {"pending_reason", "proposed_entity"} & _cols(eng, "task_events")
