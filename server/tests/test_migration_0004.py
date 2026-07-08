"""Integration test for migration 0004.

Boots a fresh sqlite db, migrates to head (0001 → 0002 → 0003 → 0004), and
asserts the Marketing default row is present with structured criteria. Also
exercises the downgrade path: rollback to 0003_buckets_v2 leaves only the
original 4 defaults and the Marketing row is gone.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


REPO_ROOT = Path(__file__).resolve().parents[1]


def _alembic_cfg(db_url: str) -> Config:
    # Same shape as test_migration_0003: override DATABASE_URL + bust the
    # lru_cache so migrations/env.py sees the temp sqlite path.
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
    # Pinned to this migration's own revision (not "head") — same rationale
    # as test_migration_0003: this test is about 0004's behavior, not
    # whatever the buckets table looks like at whatever head currently is.
    # Load-bearing since Phase 4's 0009 drops the buckets table entirely.
    command.upgrade(cfg, "0004_marketing_bucket")
    eng = create_engine(db_url, future=True)
    yield eng, cfg
    eng.dispose()


def test_marketing_default_row_exists_after_upgrade(migrated_db):
    eng, _cfg = migrated_db
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, name, criteria, is_deleted FROM buckets WHERE user_id IS NULL"
        )).all()
    assert len(rows) == 5
    by_name = {r[1]: r for r in rows}
    assert "Marketing" in by_name
    _id, name, crit, is_del = by_name["Marketing"]
    assert "<positive>" in crit and "<nearmiss>" in crit
    assert "Example cases:" in crit
    assert is_del == 0


def test_marketing_id_is_deterministic_uuid_hex(migrated_db):
    """The migration uses uuid5 so the id is stable across down→up cycles."""
    import uuid
    expected = uuid.uuid5(
        uuid.UUID("00000000-0000-0000-0000-000000000004"), "Marketing"
    ).hex
    eng, _cfg = migrated_db
    with eng.connect() as conn:
        marketing_id = conn.execute(text(
            "SELECT id FROM buckets WHERE name='Marketing' AND user_id IS NULL"
        )).scalar_one()
    assert marketing_id == expected


def test_downgrade_removes_marketing_and_repoints_threads(migrated_db):
    eng, cfg = migrated_db

    # Seed a user + thread classified into Marketing, then downgrade and
    # confirm: (a) the Marketing bucket row is gone, (b) the thread's
    # bucket_id was NULLed (no FK violation).
    with eng.begin() as conn:
        marketing_id = conn.execute(text(
            "SELECT id FROM buckets WHERE name='Marketing' AND user_id IS NULL"
        )).scalar_one()
        conn.execute(text(
            "INSERT INTO users (id, email, created_at) VALUES "
            "('u1', 'a@b.com', '2026-04-30T00:00:00+00:00')"
        ))
        conn.execute(text(
            "INSERT INTO inbox_threads (id, user_id, gmail_id, subject, bucket_id, recent_message_id) "
            "VALUES ('t1', 'u1', 'gT_m', 's', :bid, NULL)"
        ), {"bid": marketing_id})

    command.downgrade(cfg, "0003_buckets_v2")

    with eng.connect() as conn:
        names = {r[0] for r in conn.execute(text(
            "SELECT name FROM buckets WHERE user_id IS NULL"
        )).all()}
        assert names == {"Important", "Can wait", "Auto-archive", "Newsletter"}
        bid = conn.execute(text(
            "SELECT bucket_id FROM inbox_threads WHERE id='t1'"
        )).scalar_one()
        assert bid is None
