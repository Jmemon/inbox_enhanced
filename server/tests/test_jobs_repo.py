"""task_engine.jobs_repo tests: stage derivation on create, cross-user
scoping, list_jobs ordering + active-window semantics (dismissed / terminal-
aged exclusion), needs_user toggling across stage transitions, and dismiss
idempotency (Phase 4.5 jobs surface, spec 005)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import User
from app.task_engine import jobs_repo


def _mk_user(db, uid="u1"):
    user = User(id=uid, email=f"{uid}@x.com", created_at=datetime.now(timezone.utc))
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def two_users(db):
    _mk_user(db, "u1")
    _mk_user(db, "u2")
    return db


def _mk_job(db, uid="u1", kind="creation", task_kind="bucket", goal="g"):
    return jobs_repo.create_job(db, user_id=uid, kind=kind, task_kind=task_kind, goal=goal)


# ---------------------------------------------------------------------------
# create / stage derivation
# ---------------------------------------------------------------------------


def test_create_job_creation_kind_starts_proposing(two_users):
    job = _mk_job(two_users, kind="creation")
    two_users.commit()
    assert job.id
    assert job.stage == "proposing"
    assert job.needs_user is False
    assert job.dismissed_at is None
    assert job.created_at is not None
    assert job.updated_at is not None


def test_create_job_delete_retriage_kind_starts_running(two_users):
    job = jobs_repo.create_job(two_users, user_id="u1", kind="delete_retriage")
    two_users.commit()
    assert job.stage == "running"


def test_create_job_unknown_kind_raises(two_users):
    with pytest.raises(ValueError):
        jobs_repo.create_job(two_users, user_id="u1", kind="not_a_kind")


# ---------------------------------------------------------------------------
# get_owned_job / cross-user scoping
# ---------------------------------------------------------------------------


def test_get_owned_job_invisible_to_other_user(two_users):
    job = _mk_job(two_users, uid="u1")
    two_users.commit()
    assert jobs_repo.get_owned_job(two_users, user_id="u2", job_id=job.id) is None
    got = jobs_repo.get_owned_job(two_users, user_id="u1", job_id=job.id)
    assert got is not None and got.id == job.id


def test_list_jobs_cross_user_scoping_probe(two_users):
    """User B's jobs must never be visible to user A's list_jobs call — the
    same security posture as the feeds in task_engine.repo."""
    _mk_job(two_users, uid="u1")
    _mk_job(two_users, uid="u2")
    two_users.commit()
    a_jobs = jobs_repo.list_jobs(two_users, user_id="u1")
    assert len(a_jobs) == 1
    assert all(j.user_id == "u1" for j in a_jobs)


# ---------------------------------------------------------------------------
# list_jobs ordering + active window
# ---------------------------------------------------------------------------


def test_list_jobs_newest_first(two_users):
    older = _mk_job(two_users, uid="u1")
    older.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    newer = _mk_job(two_users, uid="u1")
    two_users.commit()
    jobs = jobs_repo.list_jobs(two_users, user_id="u1")
    assert [j.id for j in jobs] == [newer.id, older.id]


def test_active_window_excludes_dismissed(two_users):
    job = _mk_job(two_users, uid="u1")
    jobs_repo.dismiss(two_users, job=job)
    two_users.commit()
    assert jobs_repo.list_jobs(two_users, user_id="u1", active_only=True) == []
    assert len(jobs_repo.list_jobs(two_users, user_id="u1", active_only=False)) == 1


def test_active_window_includes_non_terminal_regardless_of_age(two_users):
    job = _mk_job(two_users, uid="u1")
    job.stage = "backfilling"
    job.updated_at = datetime.now(timezone.utc) - timedelta(days=30)
    two_users.commit()
    jobs = jobs_repo.list_jobs(two_users, user_id="u1")
    assert [j.id for j in jobs] == [job.id]


def test_active_window_excludes_terminal_older_than_7_days(two_users):
    job = _mk_job(two_users, uid="u1")
    job.stage = "done"
    job.updated_at = datetime.now(timezone.utc) - timedelta(days=8)
    two_users.commit()
    assert jobs_repo.list_jobs(two_users, user_id="u1") == []


def test_active_window_includes_terminal_within_7_days(two_users):
    job = _mk_job(two_users, uid="u1")
    job.stage = "failed"
    job.updated_at = datetime.now(timezone.utc) - timedelta(days=1)
    two_users.commit()
    jobs = jobs_repo.list_jobs(two_users, user_id="u1")
    assert [j.id for j in jobs] == [job.id]


# ---------------------------------------------------------------------------
# update_stage / needs_user toggling
# ---------------------------------------------------------------------------


def test_update_stage_to_draft_ready_sets_needs_user(two_users):
    job = _mk_job(two_users, uid="u1")
    before = job.updated_at
    jobs_repo.update_stage(two_users, job=job, stage="draft_ready")
    # Assert pre-commit: SQLite's DateTime(timezone=True) round-trips as
    # naive on refresh, and expire_on_commit=True (this fixture's default)
    # would force exactly that refresh on next attribute access after commit,
    # making a tz-aware `before` uncomparable to a post-commit `updated_at`.
    assert job.stage == "draft_ready"
    assert job.needs_user is True
    assert job.updated_at >= before
    two_users.commit()


def test_update_stage_away_from_draft_ready_clears_needs_user(two_users):
    job = _mk_job(two_users, uid="u1")
    jobs_repo.update_stage(two_users, job=job, stage="draft_ready")
    jobs_repo.update_stage(two_users, job=job, stage="backfilling")
    two_users.commit()
    assert job.stage == "backfilling"
    assert job.needs_user is False


# ---------------------------------------------------------------------------
# update_progress / set_payload / mark_failed
# ---------------------------------------------------------------------------


def test_update_progress_sets_counters(two_users):
    job = _mk_job(two_users, uid="u1")
    jobs_repo.update_progress(two_users, job=job, scanned=10, matched=3, total=50)
    two_users.commit()
    assert (job.scanned, job.matched, job.total) == (10, 3, 50)


def test_set_payload_stores_dict(two_users):
    job = _mk_job(two_users, uid="u1")
    payload = {"name": "Travel", "criteria": "..."}
    jobs_repo.set_payload(two_users, job=job, payload=payload)
    two_users.commit()
    assert job.payload == payload


def test_mark_failed_sets_stage_and_error(two_users):
    job = _mk_job(two_users, uid="u1")
    jobs_repo.mark_failed(two_users, job=job, error="boom")
    two_users.commit()
    assert job.stage == "failed"
    assert job.error == "boom"
    assert job.needs_user is False


# ---------------------------------------------------------------------------
# dismiss idempotency
# ---------------------------------------------------------------------------


def test_dismiss_sets_dismissed_at_and_is_idempotent(two_users):
    job = _mk_job(two_users, uid="u1")
    jobs_repo.dismiss(two_users, job=job)
    two_users.commit()
    first = job.dismissed_at
    assert first is not None

    jobs_repo.dismiss(two_users, job=job)
    two_users.commit()
    assert job.dismissed_at == first
