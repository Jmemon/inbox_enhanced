"""Integration test for migration 0005.

Migrates a fresh sqlite db to head and asserts the Newsletter default row
carries the v2 criteria (mentions Marketing in the description, swapped
nearmiss is the marketing@vendor.com sale block). Then downgrades to 0004
and asserts v1 is restored (founder@startup.com nearmiss back).
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


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
    # Pinned to this migration's own revision (not "head") — same rationale
    # as test_migration_0003: this test is about 0005's behavior, not
    # whatever the buckets table looks like at whatever head currently is.
    # Load-bearing since Phase 4's 0009 drops the buckets table entirely.
    command.upgrade(cfg, "0005_newsletter_v2")
    eng = create_engine(db_url, future=True)
    yield eng, cfg
    eng.dispose()


def _read_newsletter_criteria(eng) -> str:
    with eng.connect() as conn:
        return conn.execute(text(
            "SELECT criteria FROM buckets WHERE name='Newsletter' AND user_id IS NULL"
        )).scalar_one()


def test_newsletter_row_has_v2_text_after_upgrade(migrated_db):
    eng, _cfg = migrated_db
    crit = _read_newsletter_criteria(eng)
    # v2 description references Marketing as a sibling bucket
    assert "Marketing (promotional pushes from vendors)" in crit
    # v2 swaps founder@startup.com for the sale nearmiss
    assert "marketing@vendor.com" in crit
    assert "30% off everything" in crit
    # v1 wording is gone
    assert "Opted-in marketing, content subscriptions" not in crit
    assert "founder@startup.com" not in crit


def test_downgrade_restores_v1_newsletter_text(migrated_db):
    eng, cfg = migrated_db
    command.downgrade(cfg, "0004_marketing_bucket")
    crit = _read_newsletter_criteria(eng)
    assert "Opted-in marketing, content subscriptions" in crit
    assert "founder@startup.com" in crit
    assert "Quick favor — feedback on our beta?" in crit


def test_upgrade_is_idempotent(migrated_db):
    """Re-running the upgrade after we're already at head must not break."""
    eng, cfg = migrated_db
    # Already at head from the fixture; manually re-running the SQL should
    # be a no-op (same content).
    command.downgrade(cfg, "0004_marketing_bucket")
    command.upgrade(cfg, "0005_newsletter_v2")
    command.upgrade(cfg, "0005_newsletter_v2")  # second upgrade is a noop
    crit = _read_newsletter_criteria(eng)
    assert "Marketing (promotional pushes from vendors)" in crit
