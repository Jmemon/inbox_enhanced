"""Tests for app.api.actions — Phase 5 (actions, spec 006) Task 4 HTTP
surface: task_action_rules CRUD and the task_actions lifecycle (approve/
reject/undo).

Authed-client pattern mirrors test_tasks_api.py exactly (file-backed sqlite
`authed` fixture, session cookie via app.auth.sessions, `app.workers.
tasks._publish` captured via monkeypatch since api/actions.py calls it via
the same late-bound `tasks` module reference). Gmail writes are mocked the
same way test_gmail_writes.py/test_action_tasks.py do: a MagicMock stands in
for the googleapiclient `gmail` resource, patched in via
`app.gmail.client.get_gmail_client`.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.actions import repo as actions_repo
from app.actions.rules import ActionIntent
from app.auth import sessions
from app.db.models import Base, TaskEvent, TaskThreadLink, User
from app.db.session import get_db
from app.gmail.client import WRITE_SCOPE_COMPOSE, WRITE_SCOPE_MODIFY
from app.main import app
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


def _capture_publish(monkeypatch) -> list:
    captured: list[tuple[str, str, dict]] = []

    def _fake(user_id, event, payload):
        captured.append((user_id, event, payload))

    monkeypatch.setattr("app.workers.tasks._publish", _fake)
    return captured


def _grant_scopes(TS, *, uid="u1", scopes=None):
    scopes = scopes if scopes is not None else [WRITE_SCOPE_MODIFY, WRITE_SCOPE_COMPOSE]
    db = TS()
    user = db.get(User, uid)
    user.gmail_granted_scopes = scopes
    db.commit()
    db.close()


def _mk_task(TS, *, uid="u1", name="Tracker", status="active",
            state_schema=None, kind="tracker") -> str:
    schema = state_schema if state_schema is not None else (SINGLETON_SCHEMA if kind == "tracker" else None)
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


def _seed_rule(TS, task_id, *, trigger="thread_linked", trigger_params=None,
              action_type="archive_thread", action_params=None, mode="propose") -> str:
    db = TS()
    rule = actions_repo.create_rule(
        db, task_id=task_id, trigger=trigger, trigger_params=trigger_params,
        action_type=action_type, action_params=action_params, mode=mode,
    )
    db.commit()
    rule_id = rule.id
    db.close()
    return rule_id


def _seed_applied_event(TS, task_id, *, uid="u1", thread_id="thread-1", new_value="won") -> str:
    db = TS()
    event = TaskEvent(
        id=uuid.uuid4().hex, task_id=task_id, user_id=uid, origin="llm", status="applied",
        field="stage", new_value=new_value, thread_id=thread_id, evidence_quote="q",
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)
    db.commit()
    event_id = event.id
    db.close()
    return event_id


def _seed_link_row(TS, task_id, *, uid="u1", thread_id="thread-1", state="attached") -> str:
    db = TS()
    now = datetime.now(timezone.utc)
    link = TaskThreadLink(
        id=uuid.uuid4().hex, task_id=task_id, thread_id=thread_id, user_id=uid,
        origin="llm", state=state, created_at=now, updated_at=now,
    )
    db.add(link)
    db.commit()
    link_id = link.id
    db.close()
    return link_id


def _seed_action(
    TS, task_id, *, rule_id, action_type="archive_thread", action_params=None,
    source_event_id=None, source_link_id=None, thread_id="thread-1", gmail_thread_id="gmail-thread-1",
    status=None, result=None, executed_at=None,
) -> str:
    db = TS()
    intent = ActionIntent(
        rule_id=rule_id, action_type=action_type, action_params=action_params,
        source_event_id=source_event_id, source_link_id=source_link_id,
        thread_id=thread_id, gmail_thread_id=gmail_thread_id,
    )
    action = actions_repo.insert_intent(db, task_id=task_id, intent=intent)
    db.commit()
    if status is not None:
        actions_repo.set_status(db, action=action, status=status, result=result, executed_at=executed_at)
        db.commit()
    action_id = action.id
    db.close()
    return action_id


# ---------------------------------------------------------------------------
# Rules CRUD
# ---------------------------------------------------------------------------


def test_rules_routes_require_auth():
    c = TestClient(app)
    assert c.post("/api/tasks/x/rules", json={}).status_code == 401
    assert c.get("/api/tasks/x/rules").status_code == 401
    assert c.patch("/api/tasks/x/rules/y", json={}).status_code == 401
    assert c.delete("/api/tasks/x/rules/y").status_code == 401


def test_create_thread_linked_rule_happy_path(authed, monkeypatch):
    c, TS = authed
    captured = _capture_publish(monkeypatch)
    task_id = _mk_task(TS)

    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "thread_linked", "action_type": "archive_thread", "mode": "propose",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["trigger"] == "thread_linked"
    assert body["trigger_params"] is None
    assert body["action_type"] == "archive_thread"
    assert body["mode"] == "propose"
    assert body["is_deleted"] is False

    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    assert task.version == 2  # bumped from 1
    db.close()
    assert any(e == "task_updated" for _, e, _ in captured)


def test_create_entity_entered_stage_rule_happy_path(authed):
    c, TS = authed
    task_id = _mk_task(TS)

    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "entity_entered_stage", "trigger_params": {"stage": "in_progress"},
        "action_type": "label_thread", "action_params": {"label": "Tracked"}, "mode": "auto",
    })
    assert r.status_code == 201
    assert r.json()["trigger_params"] == {"stage": "in_progress"}


def test_create_rule_bucket_kind_422(authed):
    c, TS = authed
    task_id = _mk_task(TS, kind="bucket")
    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "thread_linked", "action_type": "archive_thread", "mode": "propose",
    })
    assert r.status_code == 422
    assert r.json()["detail"] == "rules are tracker-only"


def test_create_rule_invalid_trigger_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "bogus", "action_type": "archive_thread", "mode": "propose",
    })
    assert r.status_code == 422


def test_create_rule_stage_requires_trigger_params_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "entity_entered_stage", "action_type": "archive_thread", "mode": "propose",
    })
    assert r.status_code == 422
    assert "stage" in r.json()["detail"]


def test_create_rule_invalid_stage_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "entity_entered_stage", "trigger_params": {"stage": "bogus"},
        "action_type": "archive_thread", "mode": "propose",
    })
    assert r.status_code == 422
    assert "bogus" in r.json()["detail"]


def test_create_rule_no_valid_schema_422(authed):
    c, TS = authed
    task_id = _mk_task(TS, state_schema=None)
    # Force state_schema back to None (task_repo.create_task doesn't enforce
    # the API's kind-aware schema requirement).
    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    task.state_schema = None
    db.commit()
    db.close()

    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "entity_entered_stage", "trigger_params": {"stage": "won"},
        "action_type": "archive_thread", "mode": "propose",
    })
    assert r.status_code == 422
    assert r.json()["detail"] == "task has no valid schema for stage rules"


def test_create_rule_thread_linked_rejects_trigger_params_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "thread_linked", "trigger_params": {"stage": "won"},
        "action_type": "archive_thread", "mode": "propose",
    })
    assert r.status_code == 422


def test_create_rule_invalid_action_type_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "thread_linked", "action_type": "send_email", "mode": "propose",
    })
    assert r.status_code == 422


def test_create_rule_label_thread_requires_label_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "thread_linked", "action_type": "label_thread", "mode": "propose",
    })
    assert r.status_code == 422
    assert "label" in r.json()["detail"]


@pytest.mark.parametrize("label", ["INBOX", "spam", "Trash", "Unread", "Chat"])
def test_create_rule_label_thread_system_label_conflict_422(authed, label):
    c, TS = authed
    task_id = _mk_task(TS)
    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "thread_linked", "action_type": "label_thread",
        "action_params": {"label": label}, "mode": "propose",
    })
    assert r.status_code == 422
    assert r.json()["detail"] == "label name conflicts with a Gmail system label"


def test_create_rule_draft_reply_requires_instructions_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "thread_linked", "action_type": "draft_reply",
        "action_params": {}, "mode": "propose",
    })
    assert r.status_code == 422
    assert "instructions" in r.json()["detail"]


def test_create_rule_invalid_mode_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "thread_linked", "action_type": "archive_thread", "mode": "bogus",
    })
    assert r.status_code == 422


def test_create_rule_draft_reply_cannot_auto_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    r = c.post(f"/api/tasks/{task_id}/rules", json={
        "trigger": "thread_linked", "action_type": "draft_reply",
        "action_params": {"instructions": "write something"}, "mode": "auto",
    })
    assert r.status_code == 422
    assert r.json()["detail"] == "draft_reply cannot auto-run"


def test_create_rule_other_user_task_404(authed):
    c, TS = authed
    theirs = _mk_task(TS, uid="u2")
    r = c.post(f"/api/tasks/{theirs}/rules", json={
        "trigger": "thread_linked", "action_type": "archive_thread", "mode": "propose",
    })
    assert r.status_code == 404


def test_get_rules_returns_all_columns(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id, action_type="label_thread", action_params={"label": "X"})

    r = c.get(f"/api/tasks/{task_id}/rules")
    assert r.status_code == 200
    rules = r.json()["rules"]
    assert len(rules) == 1
    row = rules[0]
    assert row["id"] == rule_id
    assert set(row.keys()) == {
        "id", "task_id", "trigger", "trigger_params", "action_type",
        "action_params", "mode", "is_deleted", "created_at",
    }


def test_get_rules_other_user_task_404(authed):
    c, TS = authed
    theirs = _mk_task(TS, uid="u2")
    r = c.get(f"/api/tasks/{theirs}/rules")
    assert r.status_code == 404


def test_get_rules_excludes_soft_deleted(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id)
    c.delete(f"/api/tasks/{task_id}/rules/{rule_id}")

    r = c.get(f"/api/tasks/{task_id}/rules")
    assert r.json()["rules"] == []


def test_patch_rule_updates_mode_bumps_version_and_publishes(authed, monkeypatch):
    c, TS = authed
    captured = _capture_publish(monkeypatch)
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id, mode="propose")

    r = c.patch(f"/api/tasks/{task_id}/rules/{rule_id}", json={"mode": "auto"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "auto"
    assert body["trigger"] == "thread_linked"  # unchanged
    assert body["action_type"] == "archive_thread"  # unchanged

    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    assert task.version == 2
    db.close()
    assert any(e == "task_updated" for _, e, _ in captured)


def test_patch_rule_validates_merged_result_422(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id, action_type="label_thread", action_params={"label": "Won"})

    r = c.patch(f"/api/tasks/{task_id}/rules/{rule_id}", json={"action_params": {}})
    assert r.status_code == 422
    assert "label" in r.json()["detail"]


def test_patch_rule_wrong_task_404(authed):
    c, TS = authed
    task_a = _mk_task(TS, name="A")
    task_b = _mk_task(TS, name="B")
    rule_id = _seed_rule(TS, task_a)

    r = c.patch(f"/api/tasks/{task_b}/rules/{rule_id}", json={"mode": "auto"})
    assert r.status_code == 404


def test_patch_rule_other_user_404(authed):
    c, TS = authed
    theirs = _mk_task(TS, uid="u2")
    rule_id = _seed_rule(TS, theirs)
    r = c.patch(f"/api/tasks/{theirs}/rules/{rule_id}", json={"mode": "auto"})
    assert r.status_code == 404


def test_delete_rule_soft_deletes_bumps_version_and_publishes(authed, monkeypatch):
    c, TS = authed
    captured = _capture_publish(monkeypatch)
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id)

    r = c.delete(f"/api/tasks/{task_id}/rules/{rule_id}")
    assert r.status_code == 204

    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    assert task.version == 2
    db.close()
    assert any(e == "task_updated" for _, e, _ in captured)


def test_delete_rule_idempotent_second_call_no_second_bump(authed, monkeypatch):
    c, TS = authed
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id)

    r1 = c.delete(f"/api/tasks/{task_id}/rules/{rule_id}")
    assert r1.status_code == 204
    captured = _capture_publish(monkeypatch)
    r2 = c.delete(f"/api/tasks/{task_id}/rules/{rule_id}")
    assert r2.status_code == 204
    assert captured == []  # no second publish

    db = TS()
    task = task_repo.get_owned_task(db, user_id="u1", task_id=task_id)
    assert task.version == 2  # only bumped once
    db.close()


def test_delete_rule_other_user_404(authed):
    c, TS = authed
    theirs = _mk_task(TS, uid="u2")
    rule_id = _seed_rule(TS, theirs)
    r = c.delete(f"/api/tasks/{theirs}/rules/{rule_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Action routes: approve / reject / undo
# ---------------------------------------------------------------------------


def test_action_routes_require_auth():
    c = TestClient(app)
    assert c.post("/api/actions/x/approve").status_code == 401
    assert c.post("/api/actions/x/reject").status_code == 401
    assert c.post("/api/actions/x/undo").status_code == 401


def test_approve_archive_thread_executes(authed, monkeypatch):
    c, TS = authed
    _grant_scopes(TS)
    captured = _capture_publish(monkeypatch)
    task_id = _mk_task(TS)
    ev_id = _seed_applied_event(TS, task_id)
    rule_id = _seed_rule(TS, task_id, trigger="entity_entered_stage",
                        trigger_params={"stage": "won"})
    action_id = _seed_action(TS, task_id, rule_id=rule_id, source_event_id=ev_id)

    gmail = MagicMock()
    gmail.users().threads().modify().execute.return_value = {}
    with patch("app.gmail.client.get_gmail_client", return_value=gmail):
        r = c.post(f"/api/actions/{action_id}/approve")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "executed"
    assert body["result"] == {"removed_label_ids": ["INBOX"]}
    assert (
        "u1", "action_updated", {"task_id": task_id},
    ) in captured


def test_approve_non_proposed_409(authed):
    c, TS = authed
    _grant_scopes(TS)
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id)
    ev_id = _seed_applied_event(TS, task_id)
    action_id = _seed_action(
        TS, task_id, rule_id=rule_id, source_event_id=ev_id, status="rejected",
    )

    r = c.post(f"/api/actions/{action_id}/approve")
    assert r.status_code == 409
    assert r.json()["detail"] == "action is not pending"


def test_approve_action_with_invalid_source_returns_409(authed):
    """The action's source_event_id points at a TaskEvent that's since been
    reverted (status != 'applied') -- execute_action_inner's own re-check
    flips the row to 'rejected'; the route surfaces that as 409 rather than
    200."""
    c, TS = authed
    _grant_scopes(TS)
    task_id = _mk_task(TS)
    ev_id = _seed_applied_event(TS, task_id)
    rule_id = _seed_rule(TS, task_id, trigger="entity_entered_stage", trigger_params={"stage": "won"})
    action_id = _seed_action(TS, task_id, rule_id=rule_id, source_event_id=ev_id)

    db = TS()
    event = db.get(TaskEvent, ev_id)
    event.status = "reverted"
    db.commit()
    db.close()

    r = c.post(f"/api/actions/{action_id}/approve")
    assert r.status_code == 409
    assert r.json()["detail"] == "action source no longer valid"

    db2 = TS()
    action = actions_repo.get_owned_action(db2, user_id="u1", action_id=action_id)
    assert action.status == "rejected"
    db2.close()


def test_approve_missing_scopes_returns_200_with_failed_status(authed):
    c, TS = authed
    # No _grant_scopes call -- gmail_granted_scopes stays NULL.
    task_id = _mk_task(TS)
    ev_id = _seed_applied_event(TS, task_id)
    rule_id = _seed_rule(TS, task_id, trigger="entity_entered_stage", trigger_params={"stage": "won"})
    action_id = _seed_action(TS, task_id, rule_id=rule_id, source_event_id=ev_id)

    r = c.post(f"/api/actions/{action_id}/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert "needs permission" in body["error"]


def test_approve_unexpected_exception_500s_and_records_failure(authed):
    c, TS = authed
    _grant_scopes(TS)
    task_id = _mk_task(TS)
    ev_id = _seed_applied_event(TS, task_id)
    rule_id = _seed_rule(TS, task_id, trigger="entity_entered_stage", trigger_params={"stage": "won"})
    action_id = _seed_action(TS, task_id, rule_id=rule_id, source_event_id=ev_id)

    with patch("app.gmail.client.get_gmail_client", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            c.post(f"/api/actions/{action_id}/approve")

    db = TS()
    action = actions_repo.get_owned_action(db, user_id="u1", action_id=action_id)
    assert action.status == "failed"
    assert "boom" in action.error
    db.close()


def test_approve_other_user_404(authed):
    c, TS = authed
    theirs = _mk_task(TS, uid="u2")
    rule_id = _seed_rule(TS, theirs)
    ev_id = _seed_applied_event(TS, theirs, uid="u2")
    action_id = _seed_action(TS, theirs, rule_id=rule_id, source_event_id=ev_id)
    r = c.post(f"/api/actions/{action_id}/approve")
    assert r.status_code == 404


def test_reject_proposed_action(authed, monkeypatch):
    c, TS = authed
    captured = _capture_publish(monkeypatch)
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id)
    link_id = _seed_link_row(TS, task_id)
    action_id = _seed_action(TS, task_id, rule_id=rule_id, source_link_id=link_id)

    r = c.post(f"/api/actions/{action_id}/reject")
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert ("u1", "action_updated", {"task_id": task_id}) in captured


def test_reject_non_proposed_409(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id)
    link_id = _seed_link_row(TS, task_id)
    action_id = _seed_action(
        TS, task_id, rule_id=rule_id, source_link_id=link_id, status="executed",
        result={"removed_label_ids": ["INBOX"]},
    )

    r = c.post(f"/api/actions/{action_id}/reject")
    assert r.status_code == 409
    assert r.json()["detail"] == "action is not pending"


def test_reject_other_user_404(authed):
    c, TS = authed
    theirs = _mk_task(TS, uid="u2")
    rule_id = _seed_rule(TS, theirs)
    link_id = _seed_link_row(TS, theirs, uid="u2")
    action_id = _seed_action(TS, theirs, rule_id=rule_id, source_link_id=link_id)
    r = c.post(f"/api/actions/{action_id}/reject")
    assert r.status_code == 404


def test_undo_archive_thread_readds_inbox_label(authed, monkeypatch):
    c, TS = authed
    _grant_scopes(TS)
    captured = _capture_publish(monkeypatch)
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id)
    link_id = _seed_link_row(TS, task_id)
    action_id = _seed_action(
        TS, task_id, rule_id=rule_id, source_link_id=link_id, action_type="archive_thread",
        gmail_thread_id="gmail-thread-undo-1",
        status="executed", result={"removed_label_ids": ["INBOX"]},
    )

    gmail = MagicMock()
    gmail.users().threads().modify().execute.return_value = {}
    with patch("app.gmail.client.get_gmail_client", return_value=gmail):
        r = c.post(f"/api/actions/{action_id}/undo")

    assert r.status_code == 200
    assert r.json()["status"] == "undone"
    gmail.users().threads().modify.assert_called_with(
        userId="me", id="gmail-thread-undo-1", body={"addLabelIds": ["INBOX"]}
    )
    assert ("u1", "action_updated", {"task_id": task_id}) in captured


def test_undo_label_thread_removes_added_label(authed):
    c, TS = authed
    _grant_scopes(TS)
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id, action_type="label_thread", action_params={"label": "Won"})
    link_id = _seed_link_row(TS, task_id)
    action_id = _seed_action(
        TS, task_id, rule_id=rule_id, source_link_id=link_id, action_type="label_thread",
        action_params={"label": "Won"}, gmail_thread_id="gmail-thread-undo-2",
        status="executed", result={"added_label_ids": ["Label_9"], "label_id": "Label_9", "label_name": "Won"},
    )

    gmail = MagicMock()
    gmail.users().threads().modify().execute.return_value = {}
    with patch("app.gmail.client.get_gmail_client", return_value=gmail):
        r = c.post(f"/api/actions/{action_id}/undo")

    assert r.status_code == 200
    assert r.json()["status"] == "undone"
    gmail.users().threads().modify.assert_called_with(
        userId="me", id="gmail-thread-undo-2", body={"removeLabelIds": ["Label_9"]}
    )


def test_undo_non_executed_409(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id)
    link_id = _seed_link_row(TS, task_id)
    action_id = _seed_action(TS, task_id, rule_id=rule_id, source_link_id=link_id)  # still 'proposed'

    r = c.post(f"/api/actions/{action_id}/undo")
    assert r.status_code == 409
    assert r.json()["detail"] == "action cannot be undone"


def test_undo_non_reversible_action_type_409(authed):
    c, TS = authed
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id, action_type="draft_reply", action_params={"instructions": "hi"})
    link_id = _seed_link_row(TS, task_id)
    action_id = _seed_action(
        TS, task_id, rule_id=rule_id, source_link_id=link_id, action_type="draft_reply",
        action_params={"instructions": "hi"}, status="executed", result={"draft_id": "d1"},
    )

    r = c.post(f"/api/actions/{action_id}/undo")
    assert r.status_code == 409
    assert r.json()["detail"] == "action cannot be undone"


def test_undo_missing_scopes_409(authed):
    c, TS = authed
    # No _grant_scopes call -- gmail_granted_scopes stays NULL.
    task_id = _mk_task(TS)
    rule_id = _seed_rule(TS, task_id)
    link_id = _seed_link_row(TS, task_id)
    action_id = _seed_action(
        TS, task_id, rule_id=rule_id, source_link_id=link_id, action_type="archive_thread",
        status="executed", result={"removed_label_ids": ["INBOX"]},
    )

    r = c.post(f"/api/actions/{action_id}/undo")
    assert r.status_code == 409
    assert "needs permission" in r.json()["detail"]


def test_undo_other_user_404(authed):
    c, TS = authed
    theirs = _mk_task(TS, uid="u2")
    rule_id = _seed_rule(TS, theirs)
    link_id = _seed_link_row(TS, theirs, uid="u2")
    action_id = _seed_action(
        TS, theirs, rule_id=rule_id, source_link_id=link_id, action_type="archive_thread",
        status="executed", result={"removed_label_ids": ["INBOX"]},
    )
    r = c.post(f"/api/actions/{action_id}/undo")
    assert r.status_code == 404
