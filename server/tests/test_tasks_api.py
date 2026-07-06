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

from datetime import datetime, timezone
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
    assert body["summary"] == {"entities": 0, "pending_reviews": 0, "last_event_at": None}

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


def test_get_task_404_other_user_or_missing(authed):
    c, TS = authed
    other_id = _mk_task(TS, uid="u2")
    assert c.get(f"/api/tasks/{other_id}").status_code == 404
    assert c.get("/api/tasks/does-not-exist").status_code == 404


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
               "entity_id", "created_at"):
        assert key in event_body


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


def test_detach_other_users_thread_404(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    other_thread_id = _seed_thread(TS, uid="u2", gmail_thread_id="gE")
    r = c.delete(f"/api/tasks/{task_id}/threads/{other_thread_id}")
    assert r.status_code == 404


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
    entity = task_repo.get_entity(db, task_id=task_id, entity_id=entity_id)
    assert entity.state == {}  # never applied, so no state change


def test_reject_non_pending_409(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    ev_id, _ = _seed_pending_event(TS, task_id)
    c.post(f"/api/tasks/{task_id}/events/{ev_id}/reject")
    r2 = c.post(f"/api/tasks/{task_id}/events/{ev_id}/reject")
    assert r2.status_code == 409


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
