"""Task 10: server/app/api/tasks.py — the full draft/CRUD/board/events/
corrections router.

Authed-client pattern mirrors test_inbox_api.py/test_buckets_api.py (file-
backed sqlite `authed` fixture, session cookie via app.auth.sessions).
draft_cache is redis-backed (fakeredis, matching test_propose_task.py's
fake_redis fixture); the two celery enqueues (propose_task_draft/
backfill_task/extract_for_thread) are monkeypatched at their apply_async
call sites so no real broker is needed; `app.workers.tasks._publish` is
captured the same way test_task_engine_tasks.py does, since api/tasks.py
calls it via the same late-bound `tasks` module reference.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import sessions
from app.db.models import Base, User
from app.db.session import get_db
from app.inbox import inbox_repo
from app.main import app
from app.task_engine import draft_cache
from app.task_engine import repo as task_repo

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


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr("app.realtime.redis_client.get_redis", lambda: r)
    return r


def _capture_publish(monkeypatch) -> list:
    captured: list[tuple[str, str, dict]] = []

    def _fake(user_id, event, payload):
        captured.append((user_id, event, payload))

    monkeypatch.setattr("app.workers.tasks._publish", _fake)
    return captured


def _mk_task(TS, *, uid="u1", name="Tracker", status="active",
            state_schema=None, kind="tracker") -> str:
    schema = state_schema if state_schema is not None else SINGLETON_SCHEMA
    db = TS()
    task = task_repo.create_task(
        db, user_id=uid, name=name, goal="goal text", criteria="criteria text",
        state_schema=schema, kind=kind,
    )
    task.status = status
    db.commit()
    task_id = task.id
    db.close()
    return task_id


def _seed_thread(TS, *, uid="u1", gmail_thread_id, internal_date=1, subject="hi") -> str:
    db = TS()
    thread = inbox_repo.upsert_thread(
        db, user_id=uid, gmail_thread_id=gmail_thread_id, subject=subject, bucket_id=None,
    )
    inbox_repo.upsert_message(
        db, user_id=uid, gmail_thread_id=gmail_thread_id,
        gmail_message_id=f"m_{gmail_thread_id}",
        gmail_internal_date=internal_date, gmail_history_id=str(internal_date),
        to_addr="me@x.com", from_addr="alice@x.com", body_preview="hello",
    )
    db.commit()
    thread_id = thread.id
    db.close()
    return thread_id


def _seed_pending_event(TS, task_id, *, uid="u1", field="stage", new_value="in_progress"):
    db = TS()
    task = task_repo.get_owned_task(db, user_id=uid, task_id=task_id)
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id=uid, entity_key="_self", display_name="Self",
    )
    db.commit()
    event = task_repo.append_event(
        db, task=task, entity=entity, origin="llm", status="pending_review",
        field=field, new_value=new_value, evidence_quote="quote",
    )
    db.commit()
    event_id, entity_id = event.id, entity.id
    db.close()
    return event_id, entity_id


def test_unauth_returns_401():
    c = TestClient(app)
    assert c.get("/api/tasks").status_code == 401


# ---------------------------------------------------------------------------
# Draft: goal -> proposed schema/criteria
# ---------------------------------------------------------------------------


def test_draft_post_202_then_pending_then_ready(authed, fake_redis):
    c, TS = authed
    with patch("app.api.tasks.task_engine_tasks.propose_task_draft.apply_async") as mock_apply:
        r = c.post("/api/tasks/draft", json={"goal": "track my visa application"})
    assert r.status_code == 202
    draft_id = r.json()["draft_id"]
    mock_apply.assert_called_once_with(
        args=["u1", draft_id, "track my visa application"], countdown=0,
    )

    pending = c.get(f"/api/tasks/draft/{draft_id}")
    assert pending.status_code == 202
    assert pending.json() == {"status": "pending"}

    draft_cache.store_result(
        draft_id, user_id="u1",
        payload={"proposal": {"name": "Visa", "state_schema": SINGLETON_SCHEMA,
                              "description": "d", "keyword_probes": []},
                "positives": [], "near_misses": []},
    )
    ready = c.get(f"/api/tasks/draft/{draft_id}")
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    assert body["proposal"]["name"] == "Visa"


def test_draft_get_404_unknown(authed, fake_redis):
    c, TS = authed
    assert c.get("/api/tasks/draft/nope").status_code == 404


def test_draft_get_403_other_user(authed, fake_redis):
    c, TS = authed
    draft_cache.mark_pending("d1", user_id="u2")
    assert c.get("/api/tasks/draft/d1").status_code == 403


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_task_invalid_schema_422(authed):
    c, TS = authed
    r = c.post("/api/tasks", json={
        "name": "Job hunt", "goal": "land a job", "description": "tracks companies",
        "state_schema": {"version": 1, "pipeline": {"stages": []}},
    })
    assert r.status_code == 422
    assert "stage" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Phase 4 Task 2: kind-aware creation -- 'tracker' (default) requires a
# state_schema, 'bucket' must not have one.
# ---------------------------------------------------------------------------


def test_create_tracker_missing_schema_422(authed):
    c, TS = authed
    r = c.post("/api/tasks", json={
        "name": "Job hunt", "goal": "land a job", "description": "tracks companies",
    })
    assert r.status_code == 422
    assert r.json()["detail"] == "state_schema is required for tracker tasks"


def test_create_tracker_explicit_kind_missing_schema_422(authed):
    c, TS = authed
    r = c.post("/api/tasks", json={
        "name": "Job hunt", "goal": "land a job", "description": "tracks companies",
        "kind": "tracker",
    })
    assert r.status_code == 422
    assert r.json()["detail"] == "state_schema is required for tracker tasks"


def test_create_bucket_with_schema_422(authed):
    c, TS = authed
    r = c.post("/api/tasks", json={
        "name": "Newsletters", "goal": "organize newsletters",
        "description": "bucket for newsletters", "kind": "bucket",
        "state_schema": SINGLETON_SCHEMA,
    })
    assert r.status_code == 422
    assert r.json()["detail"] == "bucket tasks cannot have a state_schema"


def test_create_bucket_success_has_null_schema_and_enqueues_backfill(authed, monkeypatch):
    c, TS = authed
    captured = _capture_publish(monkeypatch)
    with patch("app.api.tasks.task_engine_tasks.backfill_task.apply_async") as mock_apply:
        r = c.post("/api/tasks", json={
            "name": "Newsletters", "goal": "organize newsletters",
            "description": "bucket for newsletters", "kind": "bucket",
        })
    assert r.status_code == 201
    body = r.json()
    assert body["kind"] == "bucket"
    assert body["state_schema"] is None
    assert body["status"] == "active"

    # keyword_probes defaults to [] when omitted -- same field as the tracker
    # create path, just unused by a bucket's own wizard.
    mock_apply.assert_called_once_with(
        args=["u1", body["id"], []], countdown=0,
    )
    assert len(captured) == 1
    assert captured[0] == ("u1", "task_updated",
                          {"task_id": body["id"], "version": 1, "pending_count": 0})


def test_create_task_success_enqueues_backfill_and_publishes(authed, monkeypatch):
    c, TS = authed
    captured = _capture_publish(monkeypatch)
    with patch("app.api.tasks.task_engine_tasks.backfill_task.apply_async") as mock_apply:
        r = c.post("/api/tasks", json={
            "name": "Job hunt", "goal": "land a job", "description": "tracks companies",
            "state_schema": SINGLETON_SCHEMA, "keyword_probes": ["interview", "offer"],
            "confirmed_positives": [{"sender": "hr@corp.com", "subject": "interview",
                                    "snippet": "let's schedule", "rationale": "recruiter outreach"}],
        })
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Job hunt"
    assert body["kind"] == "tracker"
    assert body["status"] == "active"
    assert body["version"] == 1
    assert body["summary"] == {
        "entities": 0, "pending_reviews": 0, "last_event_at": None, "stage_counts": {},
    }

    mock_apply.assert_called_once_with(
        args=["u1", body["id"], ["interview", "offer"]], countdown=0,
    )
    assert len(captured) == 1
    assert captured[0] == ("u1", "task_updated",
                          {"task_id": body["id"], "version": 1, "pending_count": 0})


# ---------------------------------------------------------------------------
# List / detail
# ---------------------------------------------------------------------------


def test_list_and_detail_summaries(authed):
    c, TS = authed
    task_id = _mk_task(TS, name="Tracker1")
    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="_self", display_name="Self",
    )
    db.commit()
    applied = task_repo.append_event(db, task=task, entity=entity, origin="llm",
                                     status="applied", field="stage", new_value="todo")
    task_repo.apply_event(db, task=task, entity=entity, event=applied)
    task_repo.append_event(db, task=task, entity=entity, origin="llm",
                          status="pending_review", field="stage", new_value="in_progress")
    db.commit()
    db.close()

    r = c.get("/api/tasks")
    assert r.status_code == 200
    item = next(t for t in r.json()["tasks"] if t["id"] == task_id)
    assert item["summary"]["entities"] == 1
    assert item["summary"]["pending_reviews"] == 1
    assert item["summary"]["last_event_at"] is not None

    detail = c.get(f"/api/tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["state_schema"]["pipeline"]["stages"] == ["todo", "in_progress"]
    assert detail.json()["summary"]["entities"] == 1


def test_detail_exposes_criteria_but_list_does_not(authed):
    """Minor #4 (final-review wave): criteria growth (spec §4.6 learning
    loop — attach/detach appending examples) must be auditable from the
    detail view, but the list view stays lean (criteria text can grow to
    EXAMPLE_CAP=30 examples and has no place in a summary row)."""
    c, TS = authed
    task_id = _mk_task_with_criteria(TS, criteria="Base criteria.\n\nExample cases:\n<positive>...</positive>")

    detail = c.get(f"/api/tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["criteria"] == "Base criteria.\n\nExample cases:\n<positive>...</positive>"

    listing = c.get("/api/tasks")
    assert listing.status_code == 200
    item = next(t for t in listing.json()["tasks"] if t["id"] == task_id)
    assert "criteria" not in item


def test_get_task_404_other_user_or_missing(authed):
    c, TS = authed
    other_id = _mk_task(TS, uid="u2")
    assert c.get(f"/api/tasks/{other_id}").status_code == 404
    assert c.get("/api/tasks/does-not-exist").status_code == 404


# ---------------------------------------------------------------------------
# GET /api/tasks?kind= (Phase 4 Task 3): buckets share this table now
# (Phase 4 Task 1), so the list route must explicitly separate the HUD's
# task grid (tracker-only) from the bucket-backed views built on top of the
# same endpoint.
# ---------------------------------------------------------------------------


def test_list_tasks_default_and_explicit_tracker_exclude_bucket_kind(authed):
    c, TS = authed
    tracker_id = _mk_task(TS, name="Tracker1")
    bucket_id = _mk_schemaless_task(TS, name="Bucket1", kind="bucket")

    for query in ("", "?kind=tracker"):
        r = c.get(f"/api/tasks{query}")
        assert r.status_code == 200
        ids = {t["id"] for t in r.json()["tasks"]}
        assert tracker_id in ids
        assert bucket_id not in ids


def test_list_tasks_kind_bucket_includes_defaults_and_extra_fields(authed):
    c, TS = authed
    tracker_id = _mk_task(TS, name="Tracker1")
    bucket_id = _mk_schemaless_task(TS, name="Bucket1", kind="bucket")
    db = TS()
    default_bucket = task_repo.create_task(
        db, user_id=None, name="Default", goal="", criteria="default criteria",
        state_schema=None, kind="bucket",
    )
    db.commit()
    default_bucket_id = default_bucket.id
    db.close()

    r = c.get("/api/tasks?kind=bucket")
    assert r.status_code == 200
    items = {t["id"]: t for t in r.json()["tasks"]}
    assert tracker_id not in items
    assert bucket_id in items and default_bucket_id in items
    assert items[bucket_id]["criteria"] == "criteria text"
    assert items[bucket_id]["is_default"] is False
    assert items[default_bucket_id]["is_default"] is True

    # Trackers never pay the kind-conditional criteria/is_default payload.
    tracker_item = next(
        t for t in c.get("/api/tasks?kind=tracker").json()["tasks"] if t["id"] == tracker_id
    )
    assert "criteria" not in tracker_item
    assert "is_default" not in tracker_item


def test_list_tasks_invalid_kind_422(authed):
    c, TS = authed
    assert c.get("/api/tasks?kind=whatever").status_code == 422


# ---------------------------------------------------------------------------
# stage_counts (Phase 3 Task 2): a histogram folded into the summary at zero
# extra queries -- computed from the same list_entities() result _serialize_
# summary already fetches.
# ---------------------------------------------------------------------------


MULTI_STAGE_SCHEMA = {
    "version": 1,
    "entity": {"noun": "company"},
    "pipeline": {"stages": ["applied", "interview"], "terminal": ["offer", "rejected"]},
}


def _mk_schemaless_task(TS, *, uid="u1", name="Schemaless", kind="tracker") -> str:
    """Unlike _mk_task, a None state_schema here is NOT replaced with the
    SINGLETON_SCHEMA default -- this seeds a genuinely schema-less task
    (Phase 4's bucket-kind, or a tracker whose schema proposal never
    completed)."""
    db = TS()
    task = task_repo.create_task(
        db, user_id=uid, name=name, goal="goal text", criteria="criteria text",
        state_schema=None, kind=kind,
    )
    db.commit()
    task_id = task.id
    db.close()
    return task_id


def _seed_entity_with_stage(TS, task_id, *, entity_key, stage, uid="u1"):
    """Create one entity and, if `stage` is not None, fold a single applied
    'stage' event into it so entity.state["stage"] == stage. stage=None
    leaves the entity freshly-minted with empty state (no "stage" key)."""
    db = TS()
    task = task_repo.get_owned_task(db, user_id=uid, task_id=task_id)
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id=uid, entity_key=entity_key, display_name=entity_key,
    )
    db.commit()
    if stage is not None:
        event = task_repo.append_event(db, task=task, entity=entity, origin="llm",
                                       status="applied", field="stage", new_value=stage)
        task_repo.apply_event(db, task=task, entity=entity, event=event)
        db.commit()
    entity_id = entity.id
    db.close()
    return entity_id


def test_stage_counts_empty_task_is_empty_dict(authed):
    c, TS = authed
    task_id = _mk_task(TS, state_schema=MULTI_STAGE_SCHEMA)
    r = c.get(f"/api/tasks/{task_id}")
    assert r.json()["summary"]["stage_counts"] == {}


def test_stage_counts_matches_seeded_entities_and_follows_schema_order(authed):
    c, TS = authed
    task_id = _mk_task(TS, state_schema=MULTI_STAGE_SCHEMA)
    # Seeded out of schema order (offer before applied/interview) -- the
    # returned key order must follow the SCHEMA's stage order, not insertion
    # order.
    _seed_entity_with_stage(TS, task_id, entity_key="d", stage="offer")
    _seed_entity_with_stage(TS, task_id, entity_key="a", stage="interview")
    _seed_entity_with_stage(TS, task_id, entity_key="b", stage="applied")
    _seed_entity_with_stage(TS, task_id, entity_key="c", stage="interview")

    r = c.get(f"/api/tasks/{task_id}")
    stage_counts = r.json()["summary"]["stage_counts"]
    assert stage_counts == {"applied": 1, "interview": 2, "offer": 1}
    assert list(stage_counts.keys()) == ["applied", "interview", "offer"]
    # "rejected" is a valid terminal schema stage but has zero entities --
    # it must not appear as a zero-count key.
    assert "rejected" not in stage_counts

    listing = c.get("/api/tasks")
    item = next(t for t in listing.json()["tasks"] if t["id"] == task_id)
    assert item["summary"]["stage_counts"] == stage_counts


def test_stage_counts_no_stage_bucket(authed):
    c, TS = authed
    task_id = _mk_task(TS, state_schema=MULTI_STAGE_SCHEMA)
    _seed_entity_with_stage(TS, task_id, entity_key="a", stage="applied")
    _seed_entity_with_stage(TS, task_id, entity_key="b", stage=None)
    _seed_entity_with_stage(TS, task_id, entity_key="c", stage=None)

    r = c.get(f"/api/tasks/{task_id}")
    stage_counts = r.json()["summary"]["stage_counts"]
    assert stage_counts == {"applied": 1, "(no stage)": 2}
    assert list(stage_counts.keys()) == ["applied", "(no stage)"]


def test_stage_counts_schemaless_task_uses_observed_order(authed):
    c, TS = authed
    task_id = _mk_schemaless_task(TS)
    # "a" is seeded (and folded) first so it's the OLDER entity;
    # list_entities returns most-recently-updated first, so "b" (newer)
    # surfaces before "a" despite "alpha" < "zeta" alphabetically -- this
    # pins observed order to list_entities' own order, not any sort.
    _seed_entity_with_stage(TS, task_id, entity_key="a", stage="alpha")
    _seed_entity_with_stage(TS, task_id, entity_key="b", stage="zeta")

    r = c.get(f"/api/tasks/{task_id}")
    stage_counts = r.json()["summary"]["stage_counts"]
    assert stage_counts == {"zeta": 1, "alpha": 1}
    assert list(stage_counts.keys()) == ["zeta", "alpha"]


def test_stage_counts_corrupt_schema_falls_back_to_observed_order_not_500(authed):
    """A hand-corrupted (or buggy-migration-written) state_schema must not
    500 the task list/detail -- schema.validate_schema raises ValueError on
    it (see workers/task_engine_tasks.process_task_updates's own per-task
    isolation for this exact failure mode), and the summary degrades to
    observed order instead."""
    c, TS = authed
    task_id = _mk_task(TS, state_schema=MULTI_STAGE_SCHEMA)
    _seed_entity_with_stage(TS, task_id, entity_key="a", stage="applied")

    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    task.state_schema = {"version": 1, "pipeline": {"stages": []}}  # invalid: no stages
    db.commit()
    db.close()

    r = c.get(f"/api/tasks/{task_id}")
    assert r.status_code == 200
    assert r.json()["summary"]["stage_counts"] == {"applied": 1}

    listing = c.get("/api/tasks")
    assert listing.status_code == 200


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


def test_patch_additive_schema_change_bumps_version(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    new_schema = {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["todo", "in_progress", "review"], "terminal": ["done"]},
    }
    r = c.patch(f"/api/tasks/{task_id}", json={"state_schema": new_schema})
    assert r.status_code == 200
    assert r.json()["state_schema"]["pipeline"]["stages"] == ["todo", "in_progress", "review"]
    assert r.json()["version"] == 2


def test_patch_destructive_schema_change_409(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    destructive = {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["todo"], "terminal": ["done"]},  # dropped "in_progress"
    }
    r = c.patch(f"/api/tasks/{task_id}", json={"state_schema": destructive})
    assert r.status_code == 409
    assert "in_progress" in r.json()["detail"]


def test_patch_pause_and_resume(authed, monkeypatch):
    captured = _capture_publish(monkeypatch)
    c, TS = authed
    task_id = _mk_task(TS)
    r = c.patch(f"/api/tasks/{task_id}", json={"status": "paused"})
    assert r.status_code == 200
    assert r.json()["status"] == "paused"
    # version bump so a 2B client's version-gap refetch fires even though
    # only status changed (no state_schema touch on this PATCH).
    assert r.json()["version"] == 2
    assert captured[-1][2]["version"] == 2

    r2 = c.patch(f"/api/tasks/{task_id}", json={"status": "active"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "active"
    assert r2.json()["version"] == 3
    assert captured[-1][2]["version"] == 3


def test_patch_bad_status_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    r = c.patch(f"/api/tasks/{task_id}", json={"status": "bogus"})
    assert r.status_code == 422


def test_patch_name(authed, monkeypatch):
    captured = _capture_publish(monkeypatch)
    c, TS = authed
    task_id = _mk_task(TS, name="Old")
    r = c.patch(f"/api/tasks/{task_id}", json={"name": "New"})
    assert r.status_code == 200
    assert r.json()["name"] == "New"
    # name-only PATCH must still bump version — otherwise the 2B client's
    # version-gap refetch sees no gap and keeps the stale name.
    assert r.json()["version"] == 2
    assert captured[-1][2]["version"] == 2


def test_patch_twice_versions_strictly_increase(authed):
    """Two successive successful PATCHes (of any field) must each bump the
    version — the client's version-gap refetch relies on every task_updated
    carrying a version strictly greater than the last it saw."""
    c, TS = authed
    task_id = _mk_task(TS, name="Old")
    r1 = c.patch(f"/api/tasks/{task_id}", json={"name": "Mid"})
    assert r1.status_code == 200
    v1 = r1.json()["version"]

    r2 = c.patch(f"/api/tasks/{task_id}", json={"status": "paused"})
    assert r2.status_code == 200
    v2 = r2.json()["version"]

    assert v2 > v1


def test_patch_other_user_404(authed):
    c, TS = authed
    other_id = _mk_task(TS, uid="u2")
    assert c.patch(f"/api/tasks/{other_id}", json={"name": "x"}).status_code == 404


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


def test_delete_soft_deletes_and_is_idempotent(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    assert c.delete(f"/api/tasks/{task_id}").status_code == 204
    assert c.get(f"/api/tasks/{task_id}").status_code == 404
    # idempotent: second delete of the same (now soft-deleted) task is still 204
    assert c.delete(f"/api/tasks/{task_id}").status_code == 204


def test_delete_bumps_version_before_publish(authed, monkeypatch):
    """A soft-deleted task's version must bump so another session's open
    TaskDetail — which refetches its cached detail only on a version gap or
    a pending-count change — actually refetches, 404s (get_owned_task
    excludes soft-deleted rows), and evicts. Without this, that other
    session's TaskDetail would show a deleted task forever."""
    c, TS = authed
    captured = _capture_publish(monkeypatch)
    task_id = _mk_task(TS)
    db = TS()
    pre_delete_version = task_repo.get_owned_task(db, user_id="u1", task_id=task_id).version
    db.close()

    assert c.delete(f"/api/tasks/{task_id}").status_code == 204

    assert len(captured) == 1
    assert captured[0][2]["version"] > pre_delete_version

    db2 = TS()
    task = task_repo.get_owned_task_any_status(db2, user_id="u1", task_id=task_id)
    assert task.version > pre_delete_version


def test_delete_other_user_404(authed):
    c, TS = authed
    other_id = _mk_task(TS, uid="u2")
    assert c.delete(f"/api/tasks/{other_id}").status_code == 404


def test_delete_nonexistent_404(authed):
    c, TS = authed
    assert c.delete("/api/tasks/does-not-exist").status_code == 404


# ---------------------------------------------------------------------------
# Board + events feed
# ---------------------------------------------------------------------------


def test_board_returns_entities(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    db = TS()
    task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="_self", display_name="Self",
    )
    db.commit()
    db.close()

    r = c.get(f"/api/tasks/{task_id}/board")
    assert r.status_code == 200
    entities = r.json()["entities"]
    assert len(entities) == 1
    assert entities[0]["entity_key"] == "_self"
    assert "state" in entities[0] and "updated_at" in entities[0]


def test_events_feed_newest_first_with_status_filter_and_provenance_fields(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="_self", display_name="Self",
    )
    db.commit()
    ev1 = task_repo.append_event(db, task=task, entity=entity, origin="llm",
                                status="applied", field="stage", new_value="todo")
    ev2 = task_repo.append_event(db, task=task, entity=entity, origin="llm",
                                status="pending_review", field="stage", new_value="in_progress")
    db.commit()
    ev1_id, ev2_id = ev1.id, ev2.id
    db.close()

    r = c.get(f"/api/tasks/{task_id}/events")
    assert r.status_code == 200
    ids = [e["id"] for e in r.json()["events"]]
    assert ids == [ev2_id, ev1_id]  # newest first

    filtered = c.get(f"/api/tasks/{task_id}/events?status=pending_review")
    assert [e["id"] for e in filtered.json()["events"]] == [ev2_id]

    event_body = filtered.json()["events"][0]
    for key in ("field", "old_value", "new_value", "evidence_quote", "confidence",
               "origin", "status", "thread_id", "message_id", "gmail_message_id",
               "entity_id", "pending_reason", "proposed_entity", "created_at"):
        assert key in event_body


def test_events_feed_serializes_pending_reason_and_proposed_entity(authed):
    """A near_duplicate_entity pending event's provenance round-trips over
    the wire; a plain applied event carries both fields as null."""
    c, TS = authed
    task_id = _mk_task(TS)
    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="_self", display_name="Self",
    )
    db.commit()
    applied = task_repo.append_event(db, task=task, entity=entity, origin="llm",
                                     status="applied", field="stage", new_value="todo")
    pending = task_repo.append_event(
        db, task=task, entity=entity, origin="llm", status="pending_review",
        field="stage", new_value="in_progress", pending_reason="near_duplicate_entity",
        proposed_entity="Stripewise Corp",
    )
    db.commit()
    applied_id, pending_id = applied.id, pending.id
    db.close()

    body = {e["id"]: e for e in c.get(f"/api/tasks/{task_id}/events").json()["events"]}
    assert body[pending_id]["pending_reason"] == "near_duplicate_entity"
    assert body[pending_id]["proposed_entity"] == "Stripewise Corp"
    assert body[applied_id]["pending_reason"] is None
    assert body[applied_id]["proposed_entity"] is None


def test_events_feed_pagination(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="_self", display_name="Self",
    )
    db.commit()
    for i in range(3):
        task_repo.append_event(db, task=task, entity=entity, origin="llm",
                              status="applied", field="notes", new_value=str(i))
    db.commit()
    db.close()

    page1 = c.get(f"/api/tasks/{task_id}/events?limit=2&page=1")
    assert len(page1.json()["events"]) == 2
    page2 = c.get(f"/api/tasks/{task_id}/events?limit=2&page=2")
    assert len(page2.json()["events"]) == 1


# ---------------------------------------------------------------------------
# Threads: list / attach / detach
# ---------------------------------------------------------------------------


def test_list_task_threads_returns_attached(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    thread_id = _seed_thread(TS, gmail_thread_id="gD")
    db = TS()
    task_repo.upsert_link(db, task_id=task_id, thread_id=thread_id, user_id="u1",
                         origin="llm", state="attached")
    db.commit()
    db.close()

    r = c.get(f"/api/tasks/{task_id}/threads")
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["threads"]] == [thread_id]


def test_attach_thread_upserts_link_and_enqueues_extraction(authed, monkeypatch):
    c, TS = authed
    captured = _capture_publish(monkeypatch)
    task_id = _mk_task(TS)
    thread_id = _seed_thread(TS, gmail_thread_id="gA")

    with patch("app.api.tasks.task_engine_tasks.extract_for_thread.apply_async") as mock_apply:
        r = c.post(f"/api/tasks/{task_id}/threads", json={"thread_id": thread_id})
    assert r.status_code == 201
    mock_apply.assert_called_once_with(args=["u1", task_id, thread_id], countdown=0)

    db = TS()
    assert task_repo.list_attached_thread_ids(db, task_id=task_id) == {thread_id}
    assert len(captured) == 1


def test_attach_thread_bumps_version(authed, monkeypatch):
    """Attaching a thread must bump task.version — otherwise another
    session's TasksProvider (version-gap-or-pending-count-gated refetch)
    never learns this task now tracks a new thread, and an open TaskDetail
    there never sees it in its threads panel."""
    c, TS = authed
    captured = _capture_publish(monkeypatch)
    task_id = _mk_task(TS)
    thread_id = _seed_thread(TS, gmail_thread_id="gM")

    with patch("app.api.tasks.task_engine_tasks.extract_for_thread.apply_async"):
        r = c.post(f"/api/tasks/{task_id}/threads", json={"thread_id": thread_id})
    assert r.status_code == 201

    assert captured[-1][2]["version"] == 2
    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    assert task.version == 2


def test_attach_other_users_thread_404(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    other_thread_id = _seed_thread(TS, uid="u2", gmail_thread_id="gB")
    r = c.post(f"/api/tasks/{task_id}/threads", json={"thread_id": other_thread_id})
    assert r.status_code == 404


def test_detach_thread_reverts_applied_events_and_refolds_entity(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    thread_id = _seed_thread(TS, gmail_thread_id="gC")

    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    task_repo.upsert_link(db, task_id=task_id, thread_id=thread_id, user_id="u1",
                         origin="llm", state="attached")
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="_self", display_name="Self",
    )
    db.commit()
    ev = task_repo.append_event(db, task=task, entity=entity, origin="llm", status="applied",
                               field="stage", new_value="in_progress", thread_id=thread_id)
    task_repo.apply_event(db, task=task, entity=entity, event=ev)
    db.commit()
    ev_id, entity_id = ev.id, entity.id
    db.close()

    r = c.delete(f"/api/tasks/{task_id}/threads/{thread_id}")
    assert r.status_code == 204

    db2 = TS()
    reverted = task_repo.get_event(db2, task_id=task_id, event_id=ev_id)
    assert reverted.status == "reverted"
    entity2 = task_repo.get_entity(db2, task_id=task_id, entity_id=entity_id)
    assert entity2.state == {"stage": None}
    link = task_repo.get_link(db2, task_id=task_id, thread_id=thread_id)
    assert link.state == "detached"


def test_detach_thread_rejects_that_threads_pending_events_but_not_others(authed):
    """A detached thread's not-yet-reviewed proposals must not remain
    approvable later — DELETE flips this thread's pending_review events to
    rejected, but a pending event seeded off a DIFFERENT (still-attached)
    thread survives untouched."""
    c, TS = authed
    task_id = _mk_task(TS)
    detached_thread_id = _seed_thread(TS, gmail_thread_id="gF")
    other_thread_id = _seed_thread(TS, gmail_thread_id="gG")

    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    task_repo.upsert_link(db, task_id=task_id, thread_id=detached_thread_id, user_id="u1",
                         origin="llm", state="attached")
    task_repo.upsert_link(db, task_id=task_id, thread_id=other_thread_id, user_id="u1",
                         origin="llm", state="attached")
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="_self", display_name="Self",
    )
    db.commit()
    pending_on_detached = task_repo.append_event(
        db, task=task, entity=entity, origin="llm", status="pending_review",
        field="stage", new_value="in_progress", thread_id=detached_thread_id,
        pending_reason="low_confidence",
    )
    pending_on_other = task_repo.append_event(
        db, task=task, entity=entity, origin="llm", status="pending_review",
        field="notes", new_value="keep me", thread_id=other_thread_id,
        pending_reason="low_confidence",
    )
    db.commit()
    detached_ev_id, other_ev_id = pending_on_detached.id, pending_on_other.id
    db.close()

    r = c.delete(f"/api/tasks/{task_id}/threads/{detached_thread_id}")
    assert r.status_code == 204

    db2 = TS()
    assert task_repo.get_event(db2, task_id=task_id, event_id=detached_ev_id).status == "rejected"
    assert task_repo.get_event(db2, task_id=task_id, event_id=other_ev_id).status == "pending_review"


def test_detach_thread_bumps_version_even_with_no_applied_events(authed, monkeypatch):
    """A detach with nothing to revert (no applied events on this thread)
    must still bump version unconditionally — refold_entity's own
    conditional bump (see test_detach_thread_reverts_applied_events_and_
    refolds_entity) doesn't fire when there's nothing to refold, but the
    thread's attachment state still changed and other sessions' providers
    need the version gap to learn about it."""
    c, TS = authed
    captured = _capture_publish(monkeypatch)
    task_id = _mk_task(TS)
    thread_id = _seed_thread(TS, gmail_thread_id="gN")
    db = TS()
    task_repo.upsert_link(db, task_id=task_id, thread_id=thread_id, user_id="u1",
                         origin="llm", state="attached")
    db.commit()
    db.close()

    r = c.delete(f"/api/tasks/{task_id}/threads/{thread_id}")
    assert r.status_code == 204

    assert captured[-1][2]["version"] == 2
    db2 = TS()
    task = task_repo.get_owned_task(db2, user_id="u1", task_id=task_id)
    assert task.version == 2


def test_detach_other_users_thread_404(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    other_thread_id = _seed_thread(TS, uid="u2", gmail_thread_id="gE")
    r = c.delete(f"/api/tasks/{task_id}/threads/{other_thread_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# kind isolation (spec §4.1, final-review wave): bucket-kind tasks NEVER get
# task_thread_links. _require_owned_task/get_owned_task resolve a task_id
# with no kind filter, so a hand-crafted request against a bucket-kind
# task_id must be rejected explicitly by attach/detach themselves.
# ---------------------------------------------------------------------------


def test_attach_thread_to_bucket_kind_task_422(authed):
    c, TS = authed
    bucket_id = _mk_schemaless_task(TS, name="Bucket1", kind="bucket")
    thread_id = _seed_thread(TS, gmail_thread_id="gBucketAttach")

    r = c.post(f"/api/tasks/{bucket_id}/threads", json={"thread_id": thread_id})
    assert r.status_code == 422

    db = TS()
    assert task_repo.list_attached_thread_ids(db, task_id=bucket_id) == set()


def test_detach_thread_from_bucket_kind_task_422(authed):
    c, TS = authed
    bucket_id = _mk_schemaless_task(TS, name="Bucket1", kind="bucket")
    thread_id = _seed_thread(TS, gmail_thread_id="gBucketDetach")

    r = c.delete(f"/api/tasks/{bucket_id}/threads/{thread_id}")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Task 2 (spec §4.6 learning loop): attach/detach feed corrections into
# task.criteria as tagged examples
# ---------------------------------------------------------------------------


def _mk_task_with_criteria(TS, *, criteria: str, uid="u1") -> str:
    db = TS()
    task = task_repo.create_task(
        db, user_id=uid, name="Tracker", goal="goal text", criteria=criteria,
        state_schema=SINGLETON_SCHEMA, kind="tracker",
    )
    db.commit()
    task_id = task.id
    db.close()
    return task_id


def test_attach_thread_appends_positive_example_from_recent_message(authed):
    c, TS = authed
    task_id = _mk_task_with_criteria(TS, criteria="Base criteria.\n\nExample cases:\n")
    thread_id = _seed_thread(TS, gmail_thread_id="gH", subject="quarterly report")

    with patch("app.api.tasks.task_engine_tasks.extract_for_thread.apply_async"):
        r = c.post(f"/api/tasks/{task_id}/threads", json={"thread_id": thread_id})
    assert r.status_code == 201

    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    assert "<positive>" in task.criteria
    assert "quarterly report" in task.criteria
    assert "alice@x.com" in task.criteria
    assert "user attached this thread to the task" in task.criteria
    assert "Base criteria." in task.criteria  # description preserved


def test_attach_thread_add_example_false_leaves_criteria_untouched(authed):
    c, TS = authed
    original = "Base criteria.\n\nExample cases:\n"
    task_id = _mk_task_with_criteria(TS, criteria=original)
    thread_id = _seed_thread(TS, gmail_thread_id="gI")

    with patch("app.api.tasks.task_engine_tasks.extract_for_thread.apply_async"):
        r = c.post(
            f"/api/tasks/{task_id}/threads",
            json={"thread_id": thread_id, "add_example": False},
        )
    assert r.status_code == 201

    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    assert task.criteria == original


def test_attach_thread_with_no_recent_message_skips_example_silently(authed):
    c, TS = authed
    task_id = _mk_task_with_criteria(TS, criteria="Base criteria.\n\nExample cases:\n")
    # A thread with no messages at all -> no recent_message_id to resolve.
    db = TS()
    thread = inbox_repo.upsert_thread(
        db, user_id="u1", gmail_thread_id="gJ", subject="no messages yet", bucket_id=None,
    )
    db.commit()
    thread_id = thread.id
    db.close()

    with patch("app.api.tasks.task_engine_tasks.extract_for_thread.apply_async"):
        r = c.post(f"/api/tasks/{task_id}/threads", json={"thread_id": thread_id})
    assert r.status_code == 201

    db2 = TS()
    task = task_repo.get_owned_task(db2, user_id="u1", task_id=task_id)
    assert task.criteria == "Base criteria.\n\nExample cases:\n"


def test_detach_thread_appends_nearmiss_example_from_recent_message(authed):
    c, TS = authed
    task_id = _mk_task_with_criteria(TS, criteria="Base criteria.\n\nExample cases:\n")
    thread_id = _seed_thread(TS, gmail_thread_id="gK", subject="newsletter blast")
    db = TS()
    task_repo.upsert_link(db, task_id=task_id, thread_id=thread_id, user_id="u1",
                         origin="llm", state="attached")
    db.commit()
    db.close()

    r = c.delete(f"/api/tasks/{task_id}/threads/{thread_id}")
    assert r.status_code == 204

    db2 = TS()
    task = task_repo.get_owned_task(db2, user_id="u1", task_id=task_id)
    assert "<nearmiss>" in task.criteria
    assert "newsletter blast" in task.criteria
    assert "user detached this thread from the task" in task.criteria


def test_detach_thread_add_example_false_leaves_criteria_untouched(authed):
    c, TS = authed
    original = "Base criteria.\n\nExample cases:\n"
    task_id = _mk_task_with_criteria(TS, criteria=original)
    thread_id = _seed_thread(TS, gmail_thread_id="gL")
    db = TS()
    task_repo.upsert_link(db, task_id=task_id, thread_id=thread_id, user_id="u1",
                         origin="llm", state="attached")
    db.commit()
    db.close()

    r = c.delete(f"/api/tasks/{task_id}/threads/{thread_id}?add_example=false")
    assert r.status_code == 204

    db2 = TS()
    task = task_repo.get_owned_task(db2, user_id="u1", task_id=task_id)
    assert task.criteria == original


# ---------------------------------------------------------------------------
# Event corrections: approve / reject / revert
# ---------------------------------------------------------------------------


def test_approve_pending_event_applies_and_updates_board(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, entity_id = _seed_pending_event(TS, task_id)

    r = c.post(f"/api/tasks/{task_id}/events/{ev_id}/approve")
    assert r.status_code == 200
    assert r.json()["status"] == "applied"

    db = TS()
    entity = task_repo.get_entity(db, task_id=task_id, entity_id=entity_id)
    assert entity.state["stage"] == "in_progress"


def test_approve_non_pending_409(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, _ = _seed_pending_event(TS, task_id)
    c.post(f"/api/tasks/{task_id}/events/{ev_id}/approve")
    r2 = c.post(f"/api/tasks/{task_id}/events/{ev_id}/approve")
    assert r2.status_code == 409


def test_reject_pending_event(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, entity_id = _seed_pending_event(TS, task_id)

    r = c.post(f"/api/tasks/{task_id}/events/{ev_id}/reject")
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

    db = TS()
    # 2C ledger Fix 2: this seeded pending event was the entity's entire
    # history, so rejecting it also cleans up the now-orphaned minted entity
    # (dedicated coverage: test_reject_deletes_orphaned_minted_new_entity /
    # test_reject_pending_on_entity_with_history_keeps_entity) — there's no
    # longer a row to assert an unset state on.
    assert task_repo.get_entity(db, task_id=task_id, entity_id=entity_id) is None


def test_reject_non_pending_409(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, _ = _seed_pending_event(TS, task_id)
    c.post(f"/api/tasks/{task_id}/events/{ev_id}/reject")
    r2 = c.post(f"/api/tasks/{task_id}/events/{ev_id}/reject")
    assert r2.status_code == 409


def test_reject_deletes_orphaned_minted_new_entity(authed):
    """2C ledger Fix 2: the validator mints an entity row at step 8 even for
    a pending_review outcome (both branches need a real entity_id) — so
    rejecting a pending event on a freshly-minted entity with no other
    history must not strand an empty entity on the board forever."""
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, entity_id = _seed_pending_event(TS, task_id)

    r = c.post(f"/api/tasks/{task_id}/events/{ev_id}/reject")
    assert r.status_code == 200

    db = TS()
    assert task_repo.get_entity(db, task_id=task_id, entity_id=entity_id) is None


def test_reject_pending_on_entity_with_history_keeps_entity(authed):
    """The same reject must NOT delete an entity that has other history — an
    already-applied event's folded state is real signal, not an empty mint."""
    c, TS = authed
    task_id = _mk_task(TS)
    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="_self", display_name="Self",
    )
    db.commit()
    applied = task_repo.append_event(
        db, task=task, entity=entity, origin="llm", status="applied",
        field="stage", new_value="todo",
    )
    task_repo.apply_event(db, task=task, entity=entity, event=applied)
    pending = task_repo.append_event(
        db, task=task, entity=entity, origin="llm", status="pending_review",
        field="stage", new_value="in_progress", evidence_quote="quote",
    )
    db.commit()
    ev_id, entity_id = pending.id, entity.id
    db.close()

    r = c.post(f"/api/tasks/{task_id}/events/{ev_id}/reject")
    assert r.status_code == 200

    db2 = TS()
    survivor = task_repo.get_entity(db2, task_id=task_id, entity_id=entity_id)
    assert survivor is not None
    assert survivor.state["stage"] == "todo"


def test_revert_applied_event_refolds_entity(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, entity_id = _seed_pending_event(TS, task_id)
    c.post(f"/api/tasks/{task_id}/events/{ev_id}/approve")

    r = c.post(f"/api/tasks/{task_id}/events/{ev_id}/revert")
    assert r.status_code == 200
    assert r.json()["status"] == "reverted"

    db = TS()
    entity = task_repo.get_entity(db, task_id=task_id, entity_id=entity_id)
    assert entity.state == {"stage": None}


def test_revert_non_applied_409(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, _ = _seed_pending_event(TS, task_id)
    r = c.post(f"/api/tasks/{task_id}/events/{ev_id}/revert")
    assert r.status_code == 409


def test_approve_redates_event_so_approval_survives_a_later_refold(authed):
    """2C ledger Fix 1: task_events fold ascending by created_at
    (repo.refold_entity) — approving an OLD pending event (its created_at
    stuck in the past) without re-dating it would let ANY later refold
    (revert/detach/merge touching this same entity) silently re-fold a
    newer-but-since-superseded applied event's value back on top of the
    user's explicit approval. The approve route must re-date the event to
    now() immediately before apply_event so the user's decision — happening
    right now — always sorts last on any future fold."""
    c, TS = authed
    task_id = _mk_task(TS)

    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="_self", display_name="Self",
    )
    db.commit()

    # An OLD pending event on `stage` (t1, far in the past).
    old_pending = task_repo.append_event(
        db, task=task, entity=entity, origin="llm", status="pending_review",
        field="stage", new_value="approved_value", evidence_quote="quote",
    )
    old_pending.created_at = datetime.fromtimestamp(1000, tz=timezone.utc)
    db.commit()

    # A NEWER applied event on the SAME field (t2 > t1, but still long before
    # "now") — an un-redated fold would sort this one last, clobbering the
    # approval below.
    newer_applied = task_repo.append_event(
        db, task=task, entity=entity, origin="llm", status="applied",
        field="stage", new_value="stale_value",
    )
    task_repo.apply_event(db, task=task, entity=entity, event=newer_applied)
    newer_applied.created_at = datetime.fromtimestamp(2000, tz=timezone.utc)
    db.commit()

    # A third, unrelated applied event on the SAME entity — its later revert
    # is what forces the refold that would otherwise undo the approval.
    third = task_repo.append_event(
        db, task=task, entity=entity, origin="llm", status="applied",
        field="notes", new_value="whatever",
    )
    task_repo.apply_event(db, task=task, entity=entity, event=third)
    db.commit()
    old_pending_id, third_id, entity_id = old_pending.id, third.id, entity.id
    db.close()

    r = c.post(f"/api/tasks/{task_id}/events/{old_pending_id}/approve")
    assert r.status_code == 200

    db2 = TS()
    entity2 = task_repo.get_entity(db2, task_id=task_id, entity_id=entity_id)
    assert entity2.state["stage"] == "approved_value"
    db2.close()

    # Force a refold via an unrelated revert on the SAME entity — this is
    # exactly the "later refold" (revert/detach/merge) the coordinator-pinned
    # semantics must survive.
    r2 = c.post(f"/api/tasks/{task_id}/events/{third_id}/revert")
    assert r2.status_code == 200

    db3 = TS()
    entity3 = task_repo.get_entity(db3, task_id=task_id, entity_id=entity_id)
    assert entity3.state["stage"] == "approved_value"  # survives the refold


def test_events_other_user_task_404(authed):
    c, TS = authed
    other_id = _mk_task(TS, uid="u2")
    ev_id, _ = _seed_pending_event(TS, other_id, uid="u2")
    assert c.post(f"/api/tasks/{other_id}/events/{ev_id}/approve").status_code == 404


# ---------------------------------------------------------------------------
# Manual entity state edit (the correction fence)
# ---------------------------------------------------------------------------


def test_manual_state_edit_coerces_stage_appends_user_event_and_applies(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    db = TS()
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="_self", display_name="Self",
    )
    db.commit()
    entity_id = entity.id
    db.close()

    r = c.post(f"/api/tasks/{task_id}/entities/{entity_id}/state",
              json={"field": "stage", "value": "in_progress"})
    assert r.status_code == 200
    assert r.json()["state"]["stage"] == "in_progress"

    db2 = TS()
    events = task_repo.list_events(db2, task_id=task_id)
    assert len(events) == 1
    assert events[0].origin == "user"
    assert events[0].status == "applied"


def test_manual_state_edit_bad_stage_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    db = TS()
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="_self", display_name="Self",
    )
    db.commit()
    entity_id = entity.id
    db.close()

    r = c.post(f"/api/tasks/{task_id}/entities/{entity_id}/state",
              json={"field": "stage", "value": "not-a-real-stage"})
    assert r.status_code == 422


def test_manual_state_edit_is_the_fence_anchor(authed):
    """This event must be exactly what transitions.latest_applied_user_event
    would find — that's the correction-fence contract a later LLM proposal
    with older evidence gets checked against."""
    c, TS = authed
    task_id = _mk_task(TS)
    db = TS()
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="_self", display_name="Self",
    )
    db.commit()
    entity_id = entity.id
    db.close()

    r = c.post(f"/api/tasks/{task_id}/entities/{entity_id}/state",
              json={"field": "stage", "value": "done"})
    assert r.status_code == 200

    db2 = TS()
    fence = task_repo.latest_applied_user_event(db2, entity_id=entity_id)
    assert fence is not None
    assert fence.origin == "user"
    assert fence.status == "applied"
    assert fence.new_value == "done"


def test_manual_state_edit_other_user_task_404(authed):
    c, TS = authed
    other_id = _mk_task(TS, uid="u2")
    db = TS()
    entity = task_repo.get_or_create_entity(
        db, task_id=other_id, user_id="u2", entity_key="_self", display_name="Self",
    )
    db.commit()
    entity_id = entity.id
    db.close()
    r = c.post(f"/api/tasks/{other_id}/entities/{entity_id}/state",
              json={"field": "stage", "value": "done"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def test_merge_repoints_events_refolds_winner_and_deletes_loser(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    loser = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="stripe inc", display_name="Stripe, Inc.",
    )
    winner = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="stripe", display_name="Stripe",
    )
    db.commit()
    ev = task_repo.append_event(db, task=task, entity=loser, origin="llm", status="applied",
                               field="stage", new_value="renewed")
    task_repo.apply_event(db, task=task, entity=loser, event=ev)
    db.commit()
    loser_id, winner_id, ev_id = loser.id, winner.id, ev.id
    db.close()

    r = c.post(f"/api/tasks/{task_id}/entities/{loser_id}/merge",
              json={"into_entity_id": winner_id})
    assert r.status_code == 204

    db2 = TS()
    assert task_repo.get_entity(db2, task_id=task_id, entity_id=loser_id) is None
    winner2 = task_repo.get_entity(db2, task_id=task_id, entity_id=winner_id)
    assert winner2.state["stage"] == "renewed"
    moved_event = task_repo.get_event(db2, task_id=task_id, event_id=ev_id)
    assert moved_event.entity_id == winner_id


def test_merge_into_self_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    db = TS()
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id="u1", entity_key="a", display_name="A",
    )
    db.commit()
    entity_id = entity.id
    db.close()

    r = c.post(f"/api/tasks/{task_id}/entities/{entity_id}/merge",
              json={"into_entity_id": entity_id})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Every mutation publishes task_updated with fresh values
# ---------------------------------------------------------------------------


def test_every_mutation_publishes_task_updated(authed, monkeypatch):
    captured = _capture_publish(monkeypatch)
    c, TS = authed

    with patch("app.api.tasks.task_engine_tasks.backfill_task.apply_async"):
        r = c.post("/api/tasks", json={
            "name": "T", "goal": "g", "description": "d", "state_schema": SINGLETON_SCHEMA,
        })
    assert r.status_code == 201
    task_id = r.json()["id"]
    assert len(captured) == 1
    assert captured[-1][1] == "task_updated"
    assert captured[-1][2]["task_id"] == task_id

    c.patch(f"/api/tasks/{task_id}", json={"name": "T2"})
    assert len(captured) == 2

    c.delete(f"/api/tasks/{task_id}")
    assert len(captured) == 3
    assert all(evt[1] == "task_updated" for evt in captured)


# ---------------------------------------------------------------------------
# Task 3 (Phase 3 HUD inversion): aggregated cross-task reviews + activity
# feeds. GET /api/reviews = pending_review events across every non-deleted
# task; GET /api/activity = everything else (applied/rejected/reverted).
# Both are user-scoped joins on Task, not TaskEvent.user_id directly — the
# cross-user probe below is the security-critical case per the plan.
# ---------------------------------------------------------------------------


def _seed_event(
    TS, task_id, *, uid="u1", status="pending_review", field="stage",
    new_value="in_progress", entity_key="_self", display_name="Self",
    pending_reason=None, proposed_entity=None, created_at=None, with_entity=True,
):
    db = TS()
    task = task_repo.get_owned_task(db, user_id=uid, task_id=task_id)
    entity = None
    if with_entity:
        entity = task_repo.get_or_create_entity(
            db, task_id=task_id, user_id=uid, entity_key=entity_key, display_name=display_name,
        )
        db.commit()
    event = task_repo.append_event(
        db, task=task, entity=entity, origin="llm", status=status,
        field=field, new_value=new_value, evidence_quote="quote",
        pending_reason=pending_reason, proposed_entity=proposed_entity,
    )
    if created_at is not None:
        event.created_at = created_at
    db.commit()
    event_id = event.id
    entity_id = entity.id if entity is not None else None
    db.close()
    return event_id, entity_id


def test_reviews_and_activity_require_auth():
    c = TestClient(app)
    assert c.get("/api/reviews").status_code == 401
    assert c.get("/api/activity").status_code == 401


def test_reviews_returns_pending_events_across_tasks_newest_first(authed):
    c, TS = authed
    t0 = datetime.now(timezone.utc)
    task1 = _mk_task(TS, name="T1")
    task2 = _mk_task(TS, name="T2")
    ev1, _ = _seed_event(TS, task1, created_at=t0)
    ev2, _ = _seed_event(TS, task2, created_at=t0 + timedelta(seconds=1))

    r = c.get("/api/reviews")
    assert r.status_code == 200
    ids = [item["id"] for item in r.json()["reviews"]]
    assert ids == [ev2, ev1]  # newest first, spanning both tasks


def test_reviews_includes_task_id_task_name_and_serialize_event_fields(authed):
    c, TS = authed
    task_id = _mk_task(TS, name="My Tracker")
    ev_id, _ = _seed_event(TS, task_id)

    r = c.get("/api/reviews")
    item = next(i for i in r.json()["reviews"] if i["id"] == ev_id)
    assert item["task_id"] == task_id
    assert item["task_name"] == "My Tracker"
    for key in ("field", "old_value", "new_value", "evidence_quote", "confidence",
               "origin", "status", "thread_id", "message_id", "gmail_message_id",
               "entity_id", "pending_reason", "proposed_entity", "created_at",
               "entity_display_name"):
        assert key in item


def test_reviews_excludes_other_users_events(authed):
    """The cross-user security probe: user B's pending events must never
    appear in user A's reviews feed."""
    c, TS = authed
    mine = _mk_task(TS, uid="u1")
    theirs = _mk_task(TS, uid="u2")
    my_ev, _ = _seed_event(TS, mine, uid="u1")
    their_ev, _ = _seed_event(TS, theirs, uid="u2")

    r = c.get("/api/reviews")
    ids = [i["id"] for i in r.json()["reviews"]]
    assert my_ev in ids
    assert their_ev not in ids
    assert len(ids) == 1


def test_reviews_excludes_deleted_tasks(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, _ = _seed_event(TS, task_id)
    c.delete(f"/api/tasks/{task_id}")

    r = c.get("/api/reviews")
    assert ev_id not in [i["id"] for i in r.json()["reviews"]]


def test_reviews_limit_clamps_low_and_high_instead_of_422ing(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    t0 = datetime.now(timezone.utc)
    for i in range(3):
        _seed_event(TS, task_id, entity_key=f"e{i}", new_value=str(i),
                    created_at=t0 + timedelta(seconds=i))

    low = c.get("/api/reviews?limit=0")
    assert low.status_code == 200
    assert len(low.json()["reviews"]) == 1

    high = c.get("/api/reviews?limit=100000")
    assert high.status_code == 200
    assert len(high.json()["reviews"]) == 3


def test_activity_returns_non_pending_events_across_tasks_newest_first(authed):
    c, TS = authed
    t0 = datetime.now(timezone.utc)
    task1 = _mk_task(TS, name="T1")
    task2 = _mk_task(TS, name="T2")
    pending, _ = _seed_event(TS, task1, status="pending_review", created_at=t0)
    applied, _ = _seed_event(TS, task1, status="applied",
                             created_at=t0 + timedelta(seconds=1))
    rejected, _ = _seed_event(TS, task2, status="rejected",
                              created_at=t0 + timedelta(seconds=2))

    r = c.get("/api/activity")
    assert r.status_code == 200
    ids = [i["id"] for i in r.json()["activity"]]
    assert ids == [rejected, applied]
    assert pending not in ids


def test_activity_excludes_other_users_events(authed):
    """Same cross-user security probe as reviews, for the activity feed."""
    c, TS = authed
    mine = _mk_task(TS, uid="u1")
    theirs = _mk_task(TS, uid="u2")
    my_ev, _ = _seed_event(TS, mine, uid="u1", status="applied")
    their_ev, _ = _seed_event(TS, theirs, uid="u2", status="applied")

    r = c.get("/api/activity")
    ids = [i["id"] for i in r.json()["activity"]]
    assert my_ev in ids
    assert their_ev not in ids
    assert len(ids) == 1


def test_activity_excludes_deleted_tasks(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, _ = _seed_event(TS, task_id, status="applied")
    c.delete(f"/api/tasks/{task_id}")

    r = c.get("/api/activity")
    assert ev_id not in [i["id"] for i in r.json()["activity"]]


def test_activity_limit_clamps_low_and_high_instead_of_422ing(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    t0 = datetime.now(timezone.utc)
    for i in range(3):
        _seed_event(TS, task_id, status="applied", entity_key=f"e{i}", new_value=str(i),
                    created_at=t0 + timedelta(seconds=i))

    low = c.get("/api/activity?limit=0")
    assert low.status_code == 200
    assert len(low.json()["activity"]) == 1

    high = c.get("/api/activity?limit=100000")
    assert high.status_code == 200
    assert len(high.json()["activity"]) == 3


def test_activity_default_limit_is_20(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    t0 = datetime.now(timezone.utc)
    for i in range(25):
        _seed_event(TS, task_id, status="applied", entity_key=f"e{i}", new_value=str(i),
                    created_at=t0 + timedelta(seconds=i))

    r = c.get("/api/activity")
    assert len(r.json()["activity"]) == 20


def test_reviews_default_limit_is_50(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    t0 = datetime.now(timezone.utc)
    for i in range(55):
        _seed_event(TS, task_id, entity_key=f"e{i}", new_value=str(i),
                    created_at=t0 + timedelta(seconds=i))

    r = c.get("/api/reviews")
    assert len(r.json()["reviews"]) == 50


def test_reviews_entity_display_name_resolves_from_live_entity(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, _ = _seed_event(TS, task_id, entity_key="acme", display_name="Acme Corp")

    r = c.get("/api/reviews")
    item = next(i for i in r.json()["reviews"] if i["id"] == ev_id)
    assert item["entity_display_name"] == "Acme Corp"


def test_reviews_entity_display_name_falls_back_to_proposed_entity_when_entity_gone(authed):
    """Mirrors reject's orphan-entity cleanup path (delete_entity_if_orphaned):
    an event's entity_id can point at a since-hard-deleted entity, so the
    display name must fall back to the LLM's verbatim proposed_entity string
    rather than resolving to nothing."""
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, entity_id = _seed_event(
        TS, task_id, entity_key="acme", display_name="Acme",
        pending_reason="near_duplicate_entity", proposed_entity="Stripewise Corp",
    )
    db = TS()
    entity = task_repo.get_entity(db, task_id=task_id, entity_id=entity_id)
    task_repo.delete_entity(db, entity=entity)
    db.commit()
    db.close()

    r = c.get("/api/reviews")
    item = next(i for i in r.json()["reviews"] if i["id"] == ev_id)
    assert item["entity_display_name"] == "Stripewise Corp"


def test_reviews_entity_display_name_null_when_neither_entity_nor_proposed(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, _ = _seed_event(TS, task_id, with_entity=False)

    r = c.get("/api/reviews")
    item = next(i for i in r.json()["reviews"] if i["id"] == ev_id)
    assert item["entity_display_name"] is None
