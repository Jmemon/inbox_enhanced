"""Phase 4.5 Task 3: server/app/api/jobs.py — the jobs HTTP surface
(create/list/get/confirm/dismiss). Authed-client pattern mirrors
test_tasks_api.py's own `authed` fixture (file-backed sqlite, session cookie
via app.auth.sessions). The two celery enqueues (propose_task_draft/
backfill_task) are monkeypatched at their apply_async call sites so no real
broker is needed; `app.workers.tasks._publish` is captured the same way
test_tasks_api.py does, since api/jobs.py calls it via the same late-bound
`tasks` module reference.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import sessions
from app.db.models import Base, User
from app.db.session import get_db
from app.main import app
from app.task_engine import jobs_repo

SINGLETON_SCHEMA = {
    "version": 1,
    "entity": None,
    "pipeline": {"stages": ["todo", "in_progress"], "terminal": ["done"]},
}


@pytest.fixture
def authed(tmp_path):
    eng = create_engine(f"sqlite+pysqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(eng)
    TS = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)

    def _get_db():
        s = TS()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db

    db = TS()
    db.add(User(id="u1", email="a@b.com", created_at=datetime.now(timezone.utc)))
    db.add(User(id="u2", email="c@d.com", created_at=datetime.now(timezone.utc)))
    db.commit()
    sid = sessions.create_session(db, user_id="u1", ttl_seconds=600)
    c = TestClient(app)
    c.cookies.set("session", sid)
    yield c, TS
    app.dependency_overrides.clear()
    eng.dispose()


def _capture_publish(monkeypatch) -> list:
    captured: list[tuple[str, str, dict]] = []

    def _fake(user_id, event, payload):
        captured.append((user_id, event, payload))

    monkeypatch.setattr("app.workers.tasks._publish", _fake)
    return captured


def _mk_job(TS, *, uid="u1", stage=None, payload=None, task_kind="tracker", goal="g") -> str:
    db = TS()
    job = jobs_repo.create_job(db, user_id=uid, kind="creation", task_kind=task_kind, goal=goal)
    if stage is not None:
        jobs_repo.update_stage(db, job=job, stage=stage)
    if payload is not None:
        jobs_repo.set_payload(db, job=job, payload=payload)
    db.commit()
    job_id = job.id
    db.close()
    return job_id


def test_unauth_returns_401():
    c = TestClient(app)
    assert c.get("/api/jobs").status_code == 401


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_job_202_and_row_proposing(authed):
    c, TS = authed
    with patch("app.api.jobs.task_engine_tasks.propose_task_draft.apply_async") as mock_apply:
        r = c.post("/api/jobs", json={"goal": "help me track my job search", "task_kind": "tracker"})
    assert r.status_code == 202
    job = r.json()["job"]
    assert job["stage"] == "proposing"
    assert job["task_kind"] == "tracker"
    assert job["goal"] == "help me track my job search"
    assert job["needs_user"] is False
    assert job["task_id"] is None
    assert "user_id" not in job

    mock_apply.assert_called_once_with(
        args=["u1", job["id"], "help me track my job search"], countdown=0,
    )

    db = TS()
    row = jobs_repo.get_owned_job(db, user_id="u1", job_id=job["id"])
    assert row is not None and row.stage == "proposing"
    db.close()


def test_create_job_missing_goal_422(authed):
    c, TS = authed
    r = c.post("/api/jobs", json={"goal": "", "task_kind": "tracker"})
    assert r.status_code == 422


def test_create_job_invalid_task_kind_422(authed):
    c, TS = authed
    r = c.post("/api/jobs", json={"goal": "g", "task_kind": "not-a-kind"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_jobs_default_active_only_excludes_dismissed(authed):
    c, TS = authed
    active_id = _mk_job(TS, uid="u1")
    dismissed_id = _mk_job(TS, uid="u1")
    db = TS()
    job = jobs_repo.get_owned_job(db, user_id="u1", job_id=dismissed_id)
    jobs_repo.dismiss(db, job=job)
    db.commit()
    db.close()

    r = c.get("/api/jobs")
    assert r.status_code == 200
    ids = [j["id"] for j in r.json()["jobs"]]
    assert active_id in ids
    assert dismissed_id not in ids


def test_list_jobs_active_0_includes_dismissed(authed):
    c, TS = authed
    job_id = _mk_job(TS, uid="u1")
    db = TS()
    job = jobs_repo.get_owned_job(db, user_id="u1", job_id=job_id)
    jobs_repo.dismiss(db, job=job)
    db.commit()
    db.close()

    r = c.get("/api/jobs?active=0")
    ids = [j["id"] for j in r.json()["jobs"]]
    assert job_id in ids


def test_list_jobs_cross_user_scoping_probe(authed):
    """User B's job must never show up in user A's list — same security
    posture as every other user-scoped feed in this codebase."""
    c, TS = authed
    _mk_job(TS, uid="u2")
    assert c.get("/api/jobs").json()["jobs"] == []


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


def test_get_job_404_unknown(authed):
    c, TS = authed
    assert c.get("/api/jobs/nope").status_code == 404


def test_get_job_404_other_users_job(authed):
    c, TS = authed
    other_job_id = _mk_job(TS, uid="u2")
    assert c.get(f"/api/jobs/{other_job_id}").status_code == 404


def test_get_job_returns_payload(authed):
    c, TS = authed
    payload = {
        "proposal": {"name": "X", "description": "d", "state_schema": SINGLETON_SCHEMA,
                    "keyword_probes": []},
        "positives": [], "near_misses": [],
    }
    job_id = _mk_job(TS, uid="u1", stage="draft_ready", payload=payload)
    r = c.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200
    body = r.json()["job"]
    assert body["stage"] == "draft_ready"
    assert body["needs_user"] is True
    assert body["payload"] == payload


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


def test_confirm_404_other_users_job(authed):
    c, TS = authed
    other_job_id = _mk_job(TS, uid="u2", stage="draft_ready")
    r = c.post(f"/api/jobs/{other_job_id}/confirm", json={"name": "X", "description": "d"})
    assert r.status_code == 404


def test_confirm_409_when_not_draft_ready(authed):
    c, TS = authed
    job_id = _mk_job(TS, uid="u1")  # stage='proposing' -- never advanced
    r = c.post(f"/api/jobs/{job_id}/confirm", json={"name": "X", "description": "d"})
    assert r.status_code == 409
    assert r.json()["detail"] == "job is not awaiting review"


def test_confirm_409_when_already_backfilling(authed):
    c, TS = authed
    job_id = _mk_job(TS, uid="u1", stage="backfilling")
    r = c.post(f"/api/jobs/{job_id}/confirm", json={"name": "X", "description": "d"})
    assert r.status_code == 409


def test_confirm_409_dismissed_draft_ready_job(authed, monkeypatch):
    """A dismissed draft_ready job must never confirm (spec invariant: dismissed
    draft_ready jobs simply never confirm). Prevents invisible revival of discarded
    jobs across tab boundaries."""
    c, TS = authed
    _capture_publish(monkeypatch)  # Capture publish events

    job_id = _mk_job(TS, uid="u1", stage="draft_ready", task_kind="tracker")

    # Dismiss it
    db = TS()
    job = jobs_repo.get_owned_job(db, user_id="u1", job_id=job_id)
    jobs_repo.dismiss(db, job=job)
    db.commit()
    db.close()

    # Confirm should reject the dismissed job
    with patch("app.api.jobs.task_engine_tasks.backfill_task.apply_async"):
        r = c.post(f"/api/jobs/{job_id}/confirm", json={
            "name": "X", "description": "d", "state_schema": SINGLETON_SCHEMA,
        })
    assert r.status_code == 409
    assert r.json()["detail"] == "job is not awaiting review"


def test_confirm_tracker_missing_schema_422(authed):
    c, TS = authed
    job_id = _mk_job(TS, uid="u1", stage="draft_ready", task_kind="tracker")
    r = c.post(f"/api/jobs/{job_id}/confirm", json={"name": "X", "description": "d"})
    assert r.status_code == 422
    assert r.json()["detail"] == "state_schema is required for tracker tasks"


def test_confirm_bucket_with_schema_422(authed):
    c, TS = authed
    job_id = _mk_job(TS, uid="u1", stage="draft_ready", task_kind="bucket")
    r = c.post(f"/api/jobs/{job_id}/confirm", json={
        "name": "X", "description": "d", "state_schema": SINGLETON_SCHEMA,
    })
    assert r.status_code == 422
    assert r.json()["detail"] == "bucket tasks cannot have a state_schema"


def test_confirm_tracker_happy_path_creates_task_and_enqueues_backfill(authed, monkeypatch):
    c, TS = authed
    captured = _capture_publish(monkeypatch)
    job_id = _mk_job(TS, uid="u1", stage="draft_ready", task_kind="tracker",
                     goal="track my job search")

    with patch("app.api.jobs.task_engine_tasks.backfill_task.apply_async") as mock_apply:
        r = c.post(f"/api/jobs/{job_id}/confirm", json={
            "name": "Job hunt", "description": "tracks companies",
            "state_schema": SINGLETON_SCHEMA, "keyword_probes": ["interview"],
        })
    assert r.status_code == 200
    body = r.json()
    task, job = body["task"], body["job"]
    assert task["name"] == "Job hunt"
    assert task["kind"] == "tracker"
    # goal came from the job row, not resupplied in the confirm body.
    assert task["goal"] == "track my job search"
    assert job["stage"] == "backfilling"
    assert job["task_id"] == task["id"]
    assert job["needs_user"] is False

    mock_apply.assert_called_once_with(
        args=["u1", task["id"], ["interview"]], kwargs={"job_id": job_id}, countdown=0,
    )

    events = {p[1] for p in captured}
    assert events == {"task_updated", "job_updated"}
    job_updated = next(p for p in captured if p[1] == "job_updated")
    assert job_updated == ("u1", "job_updated", {"job_id": job_id})

    db = TS()
    row = jobs_repo.get_owned_job(db, user_id="u1", job_id=job_id)
    assert row.stage == "backfilling"
    assert row.task_id == task["id"]
    db.close()


def test_confirm_bucket_happy_path_null_schema(authed, monkeypatch):
    c, TS = authed
    _capture_publish(monkeypatch)
    job_id = _mk_job(TS, uid="u1", stage="draft_ready", task_kind="bucket",
                     goal="organize newsletters")

    with patch("app.api.jobs.task_engine_tasks.backfill_task.apply_async") as mock_apply:
        r = c.post(f"/api/jobs/{job_id}/confirm", json={
            "name": "Newsletters", "description": "bucket for newsletters",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["task"]["kind"] == "bucket"
    assert body["task"]["state_schema"] is None
    # keyword_probes defaults to [] when omitted, same as POST /api/tasks.
    mock_apply.assert_called_once_with(
        args=["u1", body["task"]["id"], []], kwargs={"job_id": job_id}, countdown=0,
    )


# ---------------------------------------------------------------------------
# Dismiss
# ---------------------------------------------------------------------------


def test_dismiss_204_and_idempotent(authed):
    c, TS = authed
    job_id = _mk_job(TS, uid="u1")
    assert c.post(f"/api/jobs/{job_id}/dismiss").status_code == 204
    assert c.post(f"/api/jobs/{job_id}/dismiss").status_code == 204

    db = TS()
    row = jobs_repo.get_owned_job(db, user_id="u1", job_id=job_id)
    assert row.dismissed_at is not None
    db.close()


def test_dismiss_404_other_users_job(authed):
    c, TS = authed
    other_job_id = _mk_job(TS, uid="u2")
    assert c.post(f"/api/jobs/{other_job_id}/dismiss").status_code == 404


def test_dismiss_never_writes_stage_dismissed(authed):
    """spec §1.2: dismissal is dismissed_at-only -- 'dismissed' must never
    appear in the stage column."""
    c, TS = authed
    job_id = _mk_job(TS, uid="u1", stage="draft_ready")
    c.post(f"/api/jobs/{job_id}/dismiss")
    db = TS()
    row = jobs_repo.get_owned_job(db, user_id="u1", job_id=job_id)
    assert row.stage == "draft_ready"
    db.close()


# ---------------------------------------------------------------------------
# Old draft routes retired
# ---------------------------------------------------------------------------


def test_old_draft_routes_gone(authed):
    c, TS = authed
    assert c.post("/api/tasks/draft", json={"goal": "x"}).status_code in (404, 405)
    assert c.get("/api/tasks/draft/whatever").status_code in (404, 405)
