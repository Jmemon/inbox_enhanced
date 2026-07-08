"""Integration test for migration 0009 (bucket unification: buckets folded
into tasks(kind='bucket')).

Seeds buckets rows (one default user_id=NULL, one custom, one soft-deleted
custom) plus an inbox_threads row pointing at the custom bucket at revision
0008_pending_reason, upgrades to 0009, and asserts: task rows exist with the
SAME ids, kind='bucket', criteria/is_deleted carried, state_schema NULL; the
thread's bucket_id value is unchanged; the buckets table is gone. Also
covers downgrade recreating buckets and repointing rows back.
"""

import os
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
def pre_0009_db(tmp_path):
    db_path = tmp_path / "mig.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    cfg = _alembic_cfg(db_url)
    command.upgrade(cfg, "0008_pending_reason")

    eng = create_engine(db_url, future=True)
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, email, created_at) VALUES "
            "('u1', 'a@b.com', '2026-04-30T00:00:00+00:00')"
        ))
        conn.execute(text(
            "INSERT INTO buckets (id, user_id, name, criteria, is_deleted) VALUES "
            "('b-default', NULL, 'Important', 'default criteria', 0)"
        ))
        conn.execute(text(
            "INSERT INTO buckets (id, user_id, name, criteria, is_deleted) VALUES "
            "('b-custom', 'u1', 'Custom', 'custom criteria', 0)"
        ))
        conn.execute(text(
            "INSERT INTO buckets (id, user_id, name, criteria, is_deleted) VALUES "
            "('b-deleted', 'u1', 'Gone', 'deleted criteria', 1)"
        ))
        conn.execute(text(
            "INSERT INTO inbox_threads (id, user_id, gmail_id, subject, bucket_id, recent_message_id) "
            "VALUES ('t1', 'u1', 'gT1', 'hi', 'b-custom', NULL)"
        ))
    eng.dispose()
    return db_url, cfg


@pytest.fixture
def migrated_db(pre_0009_db):
    db_url, cfg = pre_0009_db
    command.upgrade(cfg, "0009_bucket_unification")
    eng = create_engine(db_url, future=True)
    yield eng, cfg
    eng.dispose()


def _cols(eng, table: str) -> set[str]:
    return {c["name"] for c in inspect(eng).get_columns(table)}


def test_buckets_table_is_gone(migrated_db):
    eng, _ = migrated_db
    assert not inspect(eng).has_table("buckets")


def test_task_rows_preserve_ids_kind_and_criteria(migrated_db):
    eng, _ = migrated_db
    with eng.connect() as conn:
        rows = {
            r.id: r
            for r in conn.execute(text(
                "SELECT id, user_id, kind, name, criteria, state_schema, status, "
                "version, is_deleted FROM tasks WHERE kind = 'bucket'"
            )).all()
        }
    # Superset, not exact equality: migrations 0002-0005 already seeded real
    # default buckets (Important, Can wait, Auto-archive, Newsletter,
    # Marketing) before this fixture's own rows are inserted, and the data
    # copy carries all of them over too.
    assert {"b-default", "b-custom", "b-deleted"} <= set(rows)

    default = rows["b-default"]
    assert default.user_id is None
    assert default.kind == "bucket"
    assert default.criteria == "default criteria"
    assert default.state_schema is None
    assert default.status == "active"
    assert default.version == 1
    assert default.is_deleted == 0

    custom = rows["b-custom"]
    assert custom.user_id == "u1"
    assert custom.criteria == "custom criteria"
    assert custom.is_deleted == 0

    deleted = rows["b-deleted"]
    assert deleted.is_deleted == 1


def test_inbox_threads_bucket_id_unchanged(migrated_db):
    eng, _ = migrated_db
    with eng.connect() as conn:
        bid = conn.execute(text(
            "SELECT bucket_id FROM inbox_threads WHERE id = 't1'"
        )).scalar_one()
    assert bid == "b-custom"


def test_downgrade_recreates_buckets_and_repoints(migrated_db):
    eng, cfg = migrated_db
    command.downgrade(cfg, "0008_pending_reason")
    assert inspect(eng).has_table("buckets")

    with eng.connect() as conn:
        rows = {
            r.id: r
            for r in conn.execute(text(
                "SELECT id, user_id, name, criteria, is_deleted FROM buckets"
            )).all()
        }
        bid = conn.execute(text(
            "SELECT bucket_id FROM inbox_threads WHERE id = 't1'"
        )).scalar_one()

    assert {"b-default", "b-custom", "b-deleted"} <= set(rows)
    assert rows["b-custom"].criteria == "custom criteria"
    assert rows["b-deleted"].is_deleted == 1
    assert bid == "b-custom"

    with eng.connect() as conn:
        remaining_bucket_tasks = conn.execute(text(
            "SELECT COUNT(*) FROM tasks WHERE kind = 'bucket'"
        )).scalar_one()
    assert remaining_bucket_tasks == 0
