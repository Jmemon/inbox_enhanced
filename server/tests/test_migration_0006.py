"""Integration test for migration 0006 (data floor).

Upgrades a fresh SQLite db to head and asserts the new columns/table exist.
FTS objects (tsvector generated columns, GIN, pg_trgm) are Postgres-only and
dialect-guarded in the migration — they are intentionally NOT asserted here.
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
    assert {"body_text", "labels", "is_unread", "is_deleted"} <= _cols(eng, "inbox_messages")
    assert {"is_archived", "last_activity_at"} <= _cols(eng, "inbox_threads")


def test_llm_calls_table_exists(migrated_db):
    eng, _ = migrated_db
    assert {"id", "user_id", "task_id", "stage", "model", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "cost_usd", "ttft_ms", "duration_ms", "outcome",
            "created_at"} <= _cols(eng, "llm_calls")


def test_downgrade_removes_everything(migrated_db):
    eng, cfg = migrated_db
    command.downgrade(cfg, "0005_newsletter_v2")
    assert "body_text" not in _cols(eng, "inbox_messages")
    assert "is_archived" not in _cols(eng, "inbox_threads")
    assert not inspect(eng).has_table("llm_calls")


def test_backfill_computes_max_gmail_internal_date_per_thread(tmp_path):
    """Regression coverage for the 0006 backfill UPDATE. The other tests in
    this file only ever upgrade an empty database, so the backfill's
    correlated-subquery SQL runs against zero rows and a future regression
    (e.g. swapping MAX for MIN, or losing the WHERE correlation) would pass
    silently. Here we seed pre-migration rows at 0005, upgrade to head, and
    assert the computed last_activity_at values are actually correct."""
    db_path = tmp_path / "backfill.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    cfg = _alembic_cfg(db_url)
    command.upgrade(cfg, "0005_newsletter_v2")

    now = datetime.now(timezone.utc)
    eng = create_engine(db_url, future=True)
    with eng.begin() as conn:
        # users: id, email, created_at are the NOT NULL columns (0001_initial).
        # Bind an ISO string, not a raw datetime: Python 3.12+'s sqlite3 adapter
        # deprecated its implicit datetime->str conversion (this file's only
        # recurring test-suite warning), and the column value is never read
        # back or asserted on here, so the string form loses nothing.
        conn.execute(
            text(
                "INSERT INTO users (id, email, created_at) "
                "VALUES (:id, :email, :created_at)"
            ),
            {"id": "user-1", "email": "user1@example.com", "created_at": now.isoformat()},
        )
        # inbox_threads: id, user_id, gmail_id are the NOT NULL columns (0002_inbox).
        conn.execute(
            text(
                "INSERT INTO inbox_threads (id, user_id, gmail_id) "
                "VALUES (:id, :user_id, :gmail_id)"
            ),
            [
                {"id": "thread-1", "user_id": "user-1", "gmail_id": "gmail-thread-1"},
                {"id": "thread-2", "user_id": "user-1", "gmail_id": "gmail-thread-2"},
            ],
        )
        # inbox_messages: id, thread_id, user_id, gmail_id, gmail_thread_id,
        # gmail_internal_date, gmail_history_id are the NOT NULL columns
        # (0002_inbox). Thread 1 gets three messages (max=5000); thread 2
        # gets none, so its last_activity_at must stay NULL.
        conn.execute(
            text(
                "INSERT INTO inbox_messages (id, thread_id, user_id, gmail_id, "
                "gmail_thread_id, gmail_internal_date, gmail_history_id) "
                "VALUES (:id, :thread_id, :user_id, :gmail_id, :gmail_thread_id, "
                ":gmail_internal_date, :gmail_history_id)"
            ),
            [
                {"id": "msg-1", "thread_id": "thread-1", "user_id": "user-1",
                 "gmail_id": "gmail-msg-1", "gmail_thread_id": "gmail-thread-1",
                 "gmail_internal_date": 1000, "gmail_history_id": "h1"},
                {"id": "msg-2", "thread_id": "thread-1", "user_id": "user-1",
                 "gmail_id": "gmail-msg-2", "gmail_thread_id": "gmail-thread-1",
                 "gmail_internal_date": 5000, "gmail_history_id": "h2"},
                {"id": "msg-3", "thread_id": "thread-1", "user_id": "user-1",
                 "gmail_id": "gmail-msg-3", "gmail_thread_id": "gmail-thread-1",
                 "gmail_internal_date": 3000, "gmail_history_id": "h3"},
            ],
        )
    eng.dispose()

    command.upgrade(cfg, "head")

    eng = create_engine(db_url, future=True)
    with eng.connect() as conn:
        rows = {
            row.id: row.last_activity_at
            for row in conn.execute(text("SELECT id, last_activity_at FROM inbox_threads"))
        }
    eng.dispose()

    assert rows["thread-1"] == 5000
    assert rows["thread-2"] is None
