"""Integration test for migration 0011 (task_action_rules + task_actions —
Phase 5 actions, spec 006 — plus users.gmail_granted_scopes).

Two brand-new tables and one nullable column, no data migration involved
(mirrors test_migration_0010's rationale): upgrades a fresh SQLite db from
0010_jobs to 0011_actions and asserts the tables/column exist, that
task_actions' CHECK constraint rejects both-null and both-set
source_event_id/source_link_id, that its two partial unique indexes each
block a duplicate (rule_id, source) pair while leaving other pairings free,
and that downgrading removes everything again.
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

_RULE_COLUMNS = {
    "id", "task_id", "trigger", "trigger_params", "action_type", "action_params",
    "mode", "is_deleted", "created_at",
}
_ACTION_COLUMNS = {
    "id", "task_id", "rule_id", "source_event_id", "source_link_id", "thread_id",
    "gmail_thread_id", "action_type", "action_params", "status", "result", "error",
    "created_at", "executed_at",
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
def pre_0011_db(tmp_path):
    db_path = tmp_path / "mig.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    cfg = _alembic_cfg(db_url)
    command.upgrade(cfg, "0010_jobs")
    return db_url, cfg


@pytest.fixture
def migrated_db(pre_0011_db):
    db_url, cfg = pre_0011_db
    command.upgrade(cfg, "0011_actions")
    eng = create_engine(db_url, future=True)
    yield eng, cfg
    eng.dispose()


def _cols(eng, table: str) -> set[str]:
    return {c["name"] for c in inspect(eng).get_columns(table)}


def _seed_user_task_rule(eng, now: str) -> None:
    """One user, one tracker task, one entity_entered_stage rule — the
    parent rows every task_actions probe below inserts against. source_event_
    id/source_link_id are soft pointers with no enforced FK on SQLite (see
    0009_bucket_unification's docstring), so the probes below reference
    arbitrary event/link ids without needing real task_events/
    task_thread_links rows."""
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
        conn.execute(
            text(
                "INSERT INTO task_action_rules (id, task_id, trigger, action_type, mode, "
                "is_deleted, created_at) VALUES (:id, :task_id, 'entity_entered_stage', "
                "'archive_thread', 'propose', 0, :created_at)"
            ),
            {"id": "rule-1", "task_id": "task-1", "created_at": now},
        )
        conn.execute(
            text(
                "INSERT INTO task_action_rules (id, task_id, trigger, action_type, mode, "
                "is_deleted, created_at) VALUES (:id, :task_id, 'entity_entered_stage', "
                "'archive_thread', 'propose', 0, :created_at)"
            ),
            {"id": "rule-2", "task_id": "task-1", "created_at": now},
        )


def test_new_tables_and_column_exist_after_upgrade(migrated_db):
    eng, _ = migrated_db
    assert _RULE_COLUMNS <= _cols(eng, "task_action_rules")
    assert _ACTION_COLUMNS <= _cols(eng, "task_actions")
    assert "gmail_granted_scopes" in _cols(eng, "users")


def test_check_constraint_rejects_both_null(migrated_db):
    eng, _ = migrated_db
    now = datetime.now(timezone.utc).isoformat()
    _seed_user_task_rule(eng, now)

    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO task_actions (id, task_id, rule_id, gmail_thread_id, "
                    "action_type, status, created_at) VALUES (:id, :task_id, :rule_id, "
                    "'gmail-thread-1', 'archive_thread', 'proposed', :created_at)"
                ),
                {"id": "action-both-null", "task_id": "task-1", "rule_id": "rule-1", "created_at": now},
            )


def test_check_constraint_rejects_both_set(migrated_db):
    eng, _ = migrated_db
    now = datetime.now(timezone.utc).isoformat()
    _seed_user_task_rule(eng, now)

    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO task_actions (id, task_id, rule_id, source_event_id, "
                    "source_link_id, gmail_thread_id, action_type, status, created_at) "
                    "VALUES (:id, :task_id, :rule_id, 'evt-1', 'link-1', 'gmail-thread-1', "
                    "'archive_thread', 'proposed', :created_at)"
                ),
                {"id": "action-both-set", "task_id": "task-1", "rule_id": "rule-1", "created_at": now},
            )


def test_check_constraint_allows_exactly_one_source(migrated_db):
    eng, _ = migrated_db
    now = datetime.now(timezone.utc).isoformat()
    _seed_user_task_rule(eng, now)

    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO task_actions (id, task_id, rule_id, source_event_id, "
                "gmail_thread_id, action_type, status, created_at) VALUES "
                "(:id, :task_id, :rule_id, 'evt-1', 'gmail-thread-1', 'archive_thread', "
                "'proposed', :created_at)"
            ),
            {"id": "action-event-only", "task_id": "task-1", "rule_id": "rule-1", "created_at": now},
        )
        conn.execute(
            text(
                "INSERT INTO task_actions (id, task_id, rule_id, source_link_id, "
                "gmail_thread_id, action_type, status, created_at) VALUES "
                "(:id, :task_id, :rule_id, 'link-1', 'gmail-thread-1', 'archive_thread', "
                "'proposed', :created_at)"
            ),
            {"id": "action-link-only", "task_id": "task-1", "rule_id": "rule-1", "created_at": now},
        )

    with eng.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM task_actions")).scalar_one()
        assert count == 2


def test_partial_unique_index_blocks_duplicate_rule_event_pair(migrated_db):
    eng, _ = migrated_db
    now = datetime.now(timezone.utc).isoformat()
    _seed_user_task_rule(eng, now)

    def _insert_event_action(conn, *, action_id: str, rule_id: str, event_id: str):
        conn.execute(
            text(
                "INSERT INTO task_actions (id, task_id, rule_id, source_event_id, "
                "gmail_thread_id, action_type, status, created_at) VALUES "
                "(:id, :task_id, :rule_id, :event_id, 'gmail-thread-1', 'archive_thread', "
                "'proposed', :created_at)"
            ),
            {"id": action_id, "task_id": "task-1", "rule_id": rule_id, "event_id": event_id, "created_at": now},
        )

    with eng.begin() as conn:
        _insert_event_action(conn, action_id="a1", rule_id="rule-1", event_id="evt-1")

    # Same (rule_id, source_event_id) pair a second time -> blocked.
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            _insert_event_action(conn, action_id="a2", rule_id="rule-1", event_id="evt-1")

    # A different event under the same rule is unaffected.
    with eng.begin() as conn:
        _insert_event_action(conn, action_id="a3", rule_id="rule-1", event_id="evt-2")

    # The same event under a different rule is also unaffected (index is
    # scoped to the (rule_id, source_event_id) pair, not source_event_id alone).
    with eng.begin() as conn:
        _insert_event_action(conn, action_id="a4", rule_id="rule-2", event_id="evt-1")

    with eng.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM task_actions")).scalar_one()
        assert count == 3


def test_partial_unique_index_blocks_duplicate_rule_link_pair(migrated_db):
    eng, _ = migrated_db
    now = datetime.now(timezone.utc).isoformat()
    _seed_user_task_rule(eng, now)

    def _insert_link_action(conn, *, action_id: str, rule_id: str, link_id: str):
        conn.execute(
            text(
                "INSERT INTO task_actions (id, task_id, rule_id, source_link_id, "
                "gmail_thread_id, action_type, status, created_at) VALUES "
                "(:id, :task_id, :rule_id, :link_id, 'gmail-thread-1', 'label_thread', "
                "'proposed', :created_at)"
            ),
            {"id": action_id, "task_id": "task-1", "rule_id": rule_id, "link_id": link_id, "created_at": now},
        )

    with eng.begin() as conn:
        _insert_link_action(conn, action_id="a1", rule_id="rule-1", link_id="link-1")

    # Same (rule_id, source_link_id) pair a second time -> blocked.
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            _insert_link_action(conn, action_id="a2", rule_id="rule-1", link_id="link-1")

    # A different link under the same rule is unaffected.
    with eng.begin() as conn:
        _insert_link_action(conn, action_id="a3", rule_id="rule-1", link_id="link-2")

    with eng.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM task_actions")).scalar_one()
        assert count == 2


def test_downgrade_removes_everything(migrated_db):
    eng, cfg = migrated_db
    command.downgrade(cfg, "0010_jobs")
    assert not inspect(eng).has_table("task_actions")
    assert not inspect(eng).has_table("task_action_rules")
    assert "gmail_granted_scopes" not in _cols(eng, "users")
