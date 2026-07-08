"""bucket_repo policy tests. Task-backed since Phase 4 (buckets are
tasks(kind='bucket')) — SQLAlchemy passthroughs (insert returns row,
mutation persists, etc) aren't tested — only the rules the api layer relies
on: per-user scoping, kind exclusion, soft-delete filtering, and the
criteria builder."""

from datetime import datetime, timezone
import pytest
from app.db.models import Task, User
from app.inbox import bucket_repo
from app.task_engine import repo as task_repo


@pytest.fixture
def db_with_defaults(db):
    db.add(User(id="u1", email="a@b.com", created_at=datetime.now(timezone.utc)))
    db.add(User(id="u2", email="c@d.com", created_at=datetime.now(timezone.utc)))
    db.add(Task(id="def-important", user_id=None, kind="bucket", name="Important",
                goal="", criteria="x", state_schema=None, status="active",
                version=1, is_deleted=False, created_at=datetime.now(timezone.utc)))
    db.commit()
    return db


def test_list_active_includes_user_customs_and_excludes_soft_deleted(db_with_defaults):
    bucket_repo.create_custom(db_with_defaults, user_id="u1", name="Kept", criteria="x")
    deleted = bucket_repo.create_custom(db_with_defaults, user_id="u1",
                                         name="Deleted", criteria="x")
    db_with_defaults.commit()
    bucket_repo.soft_delete(db_with_defaults, deleted)
    db_with_defaults.commit()

    names = {b.name for b in bucket_repo.list_active(db_with_defaults, user_id="u1")}
    assert names == {"Important", "Kept"}


def test_list_active_excludes_other_users_buckets(db_with_defaults):
    bucket_repo.create_custom(db_with_defaults, user_id="u2",
                              name="Theirs", criteria="x")
    db_with_defaults.commit()
    names = {b.name for b in bucket_repo.list_active(db_with_defaults, user_id="u1")}
    assert "Theirs" not in names


def test_list_active_excludes_tracker_kind_tasks(db_with_defaults):
    task_repo.create_task(db_with_defaults, user_id="u1", name="Tracker",
                          goal="", criteria="x", state_schema=None, kind="tracker")
    db_with_defaults.commit()
    names = {b.name for b in bucket_repo.list_active(db_with_defaults, user_id="u1")}
    assert "Tracker" not in names


def test_list_active_name_ordered(db_with_defaults):
    bucket_repo.create_custom(db_with_defaults, user_id="u1", name="Zeta", criteria="x")
    bucket_repo.create_custom(db_with_defaults, user_id="u1", name="Alpha", criteria="x")
    db_with_defaults.commit()
    names = [b.name for b in bucket_repo.list_active(db_with_defaults, user_id="u1")]
    assert names == sorted(names)


def test_get_by_id_returns_none_for_tracker_kind(db_with_defaults):
    tracker = task_repo.create_task(db_with_defaults, user_id="u1", name="Tracker",
                                    goal="", criteria="x", state_schema=None, kind="tracker")
    db_with_defaults.commit()
    assert bucket_repo.get_by_id(db_with_defaults, tracker.id) is None


def test_get_by_id_returns_none_for_missing(db_with_defaults):
    assert bucket_repo.get_by_id(db_with_defaults, "nope") is None


def test_get_by_id_returns_bucket_kind_task(db_with_defaults):
    row = bucket_repo.get_by_id(db_with_defaults, "def-important")
    assert row is not None and row.name == "Important"


def test_create_custom_mints_bucket_kind_task_with_null_schema_and_version_1(db_with_defaults):
    row = bucket_repo.create_custom(db_with_defaults, user_id="u1", name="New", criteria="x")
    db_with_defaults.commit()
    assert row.kind == "bucket"
    assert row.state_schema is None
    assert row.version == 1
    assert row.is_deleted is False


def test_rename_mutates_name(db_with_defaults):
    row = bucket_repo.create_custom(db_with_defaults, user_id="u1", name="Old", criteria="x")
    db_with_defaults.commit()
    bucket_repo.rename(db_with_defaults, row, "New")
    db_with_defaults.commit()
    assert row.name == "New"


def test_soft_delete_mutates_is_deleted(db_with_defaults):
    row = bucket_repo.create_custom(db_with_defaults, user_id="u1", name="Gone", criteria="x")
    db_with_defaults.commit()
    bucket_repo.soft_delete(db_with_defaults, row)
    db_with_defaults.commit()
    assert row.is_deleted is True


def test_formulate_criteria_produces_tagged_blocks_in_default_format():
    text = bucket_repo.formulate_criteria(
        description="Book club emails.",
        confirmed_positives=[{"sender": "club@b.com", "subject": "march pick",
                              "snippet": "Beloved", "rationale": "club"}],
        confirmed_negatives=[{"sender": "marketing@v.com", "subject": "sale",
                              "snippet": "20% off", "rationale": "marketing"}],
    )
    assert "Book club emails." in text
    assert "Example cases:" in text
    assert "<positive>" in text and "Beloved" in text
    assert "<nearmiss>" in text and "20% off" in text
