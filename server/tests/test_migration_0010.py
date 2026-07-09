"""Integration test for migration 0010 (jobs table — Phase 4.5 jobs surface,
spec 005). This is a brand-new table with no data migration involved, so
unlike test_migration_0009 there is nothing to seed: the test upgrades from
0009 straight to 0010 and asserts the table + its full column set exist, then
downgrades back and asserts the table is gone again.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[1]

_EXPECTED_COLUMNS = {
    "id", "user_id", "kind", "task_kind", "stage", "needs_user",
    "payload", "task_id", "goal", "scanned", "matched", "total",
    "error", "created_at", "updated_at", "dismissed_at",
}


def _alembic_cfg(db_url: str) -> Config:
    os.environ["DATABASE_URL"] = db_url
    from app.config import get_settings
    get_settings.cache_clear()
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


@pytest.fixture
def pre_0010_db(tmp_path):
    db_path = tmp_path / "mig.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    cfg = _alembic_cfg(db_url)
    command.upgrade(cfg, "0009_bucket_unification")
    return db_url, cfg


@pytest.fixture
def migrated_db(pre_0010_db):
    db_url, cfg = pre_0010_db
    command.upgrade(cfg, "0010_jobs")
    eng = create_engine(db_url, future=True)
    yield eng, cfg
    eng.dispose()


def _cols(eng, table: str) -> set[str]:
    return {c["name"] for c in inspect(eng).get_columns(table)}


def test_jobs_table_exists_with_expected_columns(migrated_db):
    eng, _ = migrated_db
    assert inspect(eng).has_table("jobs")
    assert _cols(eng, "jobs") == _EXPECTED_COLUMNS


def test_downgrade_drops_jobs_table(migrated_db):
    eng, cfg = migrated_db
    command.downgrade(cfg, "0009_bucket_unification")
    assert not inspect(eng).has_table("jobs")
