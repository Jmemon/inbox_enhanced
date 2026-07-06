"""Integration test for migration 0006 (data floor).

Upgrades a fresh SQLite db to head and asserts the new columns/table exist.
FTS objects (tsvector generated columns, GIN, pg_trgm) are Postgres-only and
dialect-guarded in the migration — they are intentionally NOT asserted here.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

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
