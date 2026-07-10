"""Tests for app.workers.action_tasks — execute_action (the Celery entrypoint
a mode='auto' rule fire enqueues) and execute_action_inner (the shared
synchronous dispatch a future approve route calls directly).

Mirrors test_task_engine_tasks.py's machinery: eager celery + a file-backed
sqlite session_factory + fakeredis + `app.workers.tasks._publish` captured
via monkeypatch (action_tasks late-imports it, same reason task_engine_tasks
does — see that module's docstring). Gmail writes are mocked the same way
test_gmail_writes.py does (MagicMock via get_gmail_client); the LLM call for
draft_reply is mocked the same way test_task_engine_tasks.py mocks
llm_client.call_messages.
"""

import os
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "1")

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.actions import repo as actions_repo
from app.actions.rules import ActionIntent
from app.db.models import Base, InboxMessage, InboxThread, Task, TaskEvent, TaskThreadLink, User
from app.gmail.client import WRITE_SCOPE_COMPOSE, WRITE_SCOPE_MODIFY
from app.llm import client as llm_client
from app.workers import action_tasks

USER_ID = "u1"
TASK_ID = "task-1"
THREAD_ID = "thread-1"
GMAIL_THREAD_ID = "gmail-thread-1"


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path}/test.db", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr("app.realtime.redis_client.get_redis", lambda: r)
    return r


@pytest.fixture(autouse=True)
def _wire_session_local(session_factory, monkeypatch):
    monkeypatch.setattr("app.workers.action_tasks.SessionLocal", session_factory)


@pytest.fixture(autouse=True)
def _reset_llm_loop():
    llm_client.reset_for_tests()
    yield
    llm_client.reset_for_tests()


def _capture_publish(monkeypatch) -> list:
    captured: list[tuple[str, str, dict]] = []

    def _fake(user_id, event, payload):
        captured.append((user_id, event, payload))

    monkeypatch.setattr("app.workers.tasks._publish", _fake)
    return captured


def _seed_user(session_factory, *, user_id=USER_ID, granted_scopes=None, email="me@example.com"):
    db = session_factory()
    db.add(User(
        id=user_id, email=email, created_at=datetime.now(timezone.utc),
        gmail_granted_scopes=granted_scopes,
    ))
    db.commit()
    db.close()


def _both_scopes():
    return [WRITE_SCOPE_MODIFY, WRITE_SCOPE_COMPOSE]


def _seed_task(session_factory, *, task_id=TASK_ID, user_id=USER_ID, goal="Land the deal") -> str:
    db = session_factory()
    db.add(Task(
        id=task_id, user_id=user_id, kind="tracker", name="Deals", goal=goal, criteria="",
        status="active", version=1, is_deleted=False, created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    db.close()
    return task_id


def _seed_event(session_factory, *, task_id=TASK_ID, status="applied", thread_id=THREAD_ID,
                 evidence_quote="the deal closed") -> str:
    db = session_factory()
    event = TaskEvent(
        id=uuid.uuid4().hex, task_id=task_id, user_id=USER_ID, origin="llm", status=status,
        field="stage", new_value="won", thread_id=thread_id, evidence_quote=evidence_quote,
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)
    db.commit()
    event_id = event.id
    db.close()
    return event_id


def _seed_thread_and_message(
    session_factory, *, thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID,
    body_text="Let's finalize the contract this week.",
):
    """draft_reply's LLM prompt is built from stored Postgres rows (never a
    Gmail refetch, per app.workers.action_tasks._load_thread_text's own
    docstring) -- tests that exercise that branch need a real InboxThread/
    InboxMessage row under the exact internal thread_id the seeded
    TaskAction points at."""
    db = session_factory()
    db.add(InboxThread(id=thread_id, user_id=USER_ID, gmail_id=gmail_thread_id, subject="Deal"))
    db.add(InboxMessage(
        id=f"im-{thread_id}", thread_id=thread_id, user_id=USER_ID,
        gmail_id=f"m-{thread_id}", gmail_thread_id=gmail_thread_id,
        gmail_internal_date=1_000_000, gmail_history_id="h1",
        to_addr="me@example.com", from_addr="them@example.com",
        body_preview=body_text[:150], body_text=body_text,
    ))
    db.commit()
    db.close()


def _seed_link(session_factory, *, task_id=TASK_ID, thread_id=THREAD_ID, state="attached") -> str:
    db = session_factory()
    now = datetime.now(timezone.utc)
    link = TaskThreadLink(
        id=uuid.uuid4().hex, task_id=task_id, thread_id=thread_id, user_id=USER_ID,
        origin="llm", state=state, created_at=now, updated_at=now,
    )
    db.add(link)
    db.commit()
    link_id = link.id
    db.close()
    return link_id


def _seed_action(
    session_factory, *, task_id=TASK_ID, action_type="archive_thread", action_params=None,
    source_event_id=None, source_link_id=None, mode="propose",
) -> str:
    db = session_factory()
    rule = actions_repo.create_rule(
        db, task_id=task_id, trigger="entity_entered_stage" if source_event_id else "thread_linked",
        trigger_params={"stage": "won"} if source_event_id else None,
        action_type=action_type, action_params=action_params, mode=mode,
    )
    intent = ActionIntent(
        rule_id=rule.id, action_type=action_type, action_params=action_params,
        source_event_id=source_event_id, source_link_id=source_link_id,
        thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID,
    )
    action = actions_repo.insert_intent(db, task_id=task_id, intent=intent)
    db.commit()
    action_id = action.id
    db.close()
    return action_id


def _get_action(session_factory, action_id):
    db = session_factory()
    action = actions_repo.get_owned_action(db, user_id=USER_ID, action_id=action_id)
    db.close()
    return action


# ---------------------------------------------------------------------------
# execute_action — happy paths
# ---------------------------------------------------------------------------


def test_execute_action_archive_thread_happy_path(session_factory, fake_redis, monkeypatch):
    _seed_user(session_factory, granted_scopes=_both_scopes())
    _seed_task(session_factory)
    ev_id = _seed_event(session_factory)
    action_id = _seed_action(session_factory, action_type="archive_thread", source_event_id=ev_id)
    captured = _capture_publish(monkeypatch)

    gmail = MagicMock()
    gmail.users().threads().modify().execute.return_value = {}
    with patch("app.gmail.client.get_gmail_client", return_value=gmail):
        action_tasks.execute_action.apply(args=[USER_ID, action_id])

    action = _get_action(session_factory, action_id)
    assert action.status == "executed"
    assert action.result == {"removed_label_ids": ["INBOX"]}
    assert action.executed_at is not None
    assert (USER_ID, "action_updated", {"task_id": TASK_ID}) in captured


def test_execute_action_label_thread_happy_path(session_factory, fake_redis, monkeypatch):
    _seed_user(session_factory, granted_scopes=_both_scopes())
    _seed_task(session_factory)
    ev_id = _seed_event(session_factory)
    action_id = _seed_action(
        session_factory, action_type="label_thread", action_params={"label": "Won"},
        source_event_id=ev_id,
    )
    _capture_publish(monkeypatch)

    gmail = MagicMock()
    gmail.users().labels().list().execute.return_value = {"labels": []}
    gmail.users().labels().create().execute.return_value = {"id": "Label_1"}
    gmail.users().threads().modify().execute.return_value = {}
    with patch("app.gmail.client.get_gmail_client", return_value=gmail):
        action_tasks.execute_action.apply(args=[USER_ID, action_id])

    action = _get_action(session_factory, action_id)
    assert action.status == "executed"
    assert action.result["label_id"] == "Label_1"


def test_execute_action_inner_draft_reply_happy_path(session_factory, fake_redis, monkeypatch):
    """draft_reply only ever runs via the synchronous execute_action_inner
    seam (a future approve route) -- the Celery entrypoint refuses it (see
    test_execute_action_draft_reply_via_celery_task_refuses_and_fails
    below)."""
    _seed_user(session_factory, granted_scopes=_both_scopes())
    _seed_task(session_factory, goal="Land the deal")
    _seed_thread_and_message(session_factory)
    ev_id = _seed_event(session_factory, evidence_quote="the deal closed")
    action_id = _seed_action(
        session_factory, action_type="draft_reply", action_params={"instructions": "thank them"},
        source_event_id=ev_id,
    )

    async def _fake_call_messages(**kwargs):
        assert kwargs["stage"] == "action"
        return "Thanks so much for closing this out!"

    monkeypatch.setattr(llm_client, "call_messages", _fake_call_messages)

    db = session_factory()
    action = actions_repo.get_owned_action(db, user_id=USER_ID, action_id=action_id)
    user = db.get(User, USER_ID)
    published = []

    gmail = MagicMock()
    thread_payload = {
        "id": GMAIL_THREAD_ID,
        "messages": [{
            "id": "m1",
            "payload": {"headers": [
                {"name": "From", "value": "them@example.com"},
                {"name": "Subject", "value": "Deal"},
                {"name": "Message-ID", "value": "<m1@mail>"},
            ]},
        }],
    }
    gmail.users().threads().get().execute.return_value = thread_payload
    gmail.users().drafts().create().execute.return_value = {"id": "draft-1"}
    with patch("app.gmail.client.get_gmail_client", return_value=gmail):
        action_tasks.execute_action_inner(
            db, user=user, action=action, publish=lambda *a: published.append(a),
        )
    db.commit()
    db.close()

    action = _get_action(session_factory, action_id)
    assert action.status == "executed"
    assert action.result == {"draft_id": "draft-1"}
    raw = gmail.users().drafts().create.call_args.kwargs["body"]["message"]["raw"]
    import base64
    assert "Thanks so much" in base64.urlsafe_b64decode(raw).decode()
    assert published == [(USER_ID, "action_updated", {"task_id": TASK_ID})]


# ---------------------------------------------------------------------------
# execute_action — draft_reply belt + suspenders
# ---------------------------------------------------------------------------


def test_execute_action_draft_reply_via_celery_task_refuses_and_fails(session_factory, fake_redis, monkeypatch):
    """Spec §6 invariant 2, third line of defense: even if a draft_reply
    action somehow got enqueued through the Celery entrypoint (which
    fire_rules only ever does for mode='auto', and draft_reply rules can
    never be mode='auto'), execute_action itself refuses rather than
    silently drafting an unreviewed reply."""
    _seed_user(session_factory, granted_scopes=_both_scopes())
    _seed_task(session_factory)
    ev_id = _seed_event(session_factory)
    action_id = _seed_action(
        session_factory, action_type="draft_reply", action_params={"instructions": "hi"},
        source_event_id=ev_id,
    )
    captured = _capture_publish(monkeypatch)

    with patch("app.gmail.client.get_gmail_client") as get_client:
        action_tasks.execute_action.apply(args=[USER_ID, action_id])

    get_client.assert_not_called()
    action = _get_action(session_factory, action_id)
    assert action.status == "failed"
    assert action.error == "draft_reply cannot auto-execute"
    assert (USER_ID, "action_updated", {"task_id": TASK_ID}) in captured


# ---------------------------------------------------------------------------
# execute_action — missing scopes
# ---------------------------------------------------------------------------


def test_execute_action_missing_scopes_marks_failed(session_factory, fake_redis, monkeypatch):
    _seed_user(session_factory, granted_scopes=None)
    _seed_task(session_factory)
    ev_id = _seed_event(session_factory)
    action_id = _seed_action(session_factory, action_type="archive_thread", source_event_id=ev_id)
    _capture_publish(monkeypatch)

    with patch("app.gmail.client.get_gmail_client") as get_client:
        action_tasks.execute_action.apply(args=[USER_ID, action_id])

    get_client.assert_not_called()
    action = _get_action(session_factory, action_id)
    assert action.status == "failed"
    assert "needs permission" in action.error


# ---------------------------------------------------------------------------
# execute_action — not found / wrong status no-ops
# ---------------------------------------------------------------------------


def test_execute_action_not_found_is_a_silent_no_op(session_factory, fake_redis, monkeypatch):
    _seed_user(session_factory, granted_scopes=_both_scopes())
    _capture_publish(monkeypatch)
    # Must not raise even though the action never existed.
    action_tasks.execute_action.apply(args=[USER_ID, "no-such-action"])


def test_execute_action_not_proposed_is_a_no_op(session_factory, fake_redis, monkeypatch):
    _seed_user(session_factory, granted_scopes=_both_scopes())
    _seed_task(session_factory)
    ev_id = _seed_event(session_factory)
    action_id = _seed_action(session_factory, action_type="archive_thread", source_event_id=ev_id)
    db = session_factory()
    action = actions_repo.get_owned_action(db, user_id=USER_ID, action_id=action_id)
    actions_repo.set_status(db, action=action, status="executed", result={}, executed_at=datetime.now(timezone.utc))
    db.commit()
    db.close()
    captured = _capture_publish(monkeypatch)

    with patch("app.gmail.client.get_gmail_client") as get_client:
        action_tasks.execute_action.apply(args=[USER_ID, action_id])

    get_client.assert_not_called()
    assert captured == []  # no-op -- no re-publish, no re-execution
    action = _get_action(session_factory, action_id)
    assert action.status == "executed"  # untouched


# ---------------------------------------------------------------------------
# source-validity re-check (spec §6 invariant 7's belt-and-suspenders half)
# ---------------------------------------------------------------------------


def test_execute_action_reverted_source_event_marks_rejected(session_factory, fake_redis, monkeypatch):
    _seed_user(session_factory, granted_scopes=_both_scopes())
    _seed_task(session_factory)
    ev_id = _seed_event(session_factory, status="applied")
    action_id = _seed_action(session_factory, action_type="archive_thread", source_event_id=ev_id)

    # The source event gets reverted AFTER the action was proposed (e.g. the
    # user reverted it before this worker ran -- normally revert_event's own
    # auto-reject would have caught this synchronously; this simulates the
    # belt-and-suspenders re-check for whatever reason that didn't happen).
    db = session_factory()
    event = db.get(TaskEvent, ev_id)
    event.status = "reverted"
    db.commit()
    db.close()
    _capture_publish(monkeypatch)

    with patch("app.gmail.client.get_gmail_client") as get_client:
        action_tasks.execute_action.apply(args=[USER_ID, action_id])

    get_client.assert_not_called()
    action = _get_action(session_factory, action_id)
    assert action.status == "rejected"


def test_execute_action_detached_source_link_marks_rejected(session_factory, fake_redis, monkeypatch):
    _seed_user(session_factory, granted_scopes=_both_scopes())
    _seed_task(session_factory)
    link_id = _seed_link(session_factory, state="attached")
    action_id = _seed_action(session_factory, action_type="archive_thread", source_link_id=link_id)

    db = session_factory()
    link = db.get(TaskThreadLink, link_id)
    link.state = "detached"
    db.commit()
    db.close()
    _capture_publish(monkeypatch)

    with patch("app.gmail.client.get_gmail_client") as get_client:
        action_tasks.execute_action.apply(args=[USER_ID, action_id])

    get_client.assert_not_called()
    action = _get_action(session_factory, action_id)
    assert action.status == "rejected"


# ---------------------------------------------------------------------------
# unexpected exception -> guarded rollback + fresh-session failure write + reraise
# ---------------------------------------------------------------------------


def test_execute_action_unexpected_exception_marks_failed_and_reraises(session_factory, fake_redis, monkeypatch):
    _seed_user(session_factory, granted_scopes=_both_scopes())
    _seed_task(session_factory)
    ev_id = _seed_event(session_factory)
    action_id = _seed_action(session_factory, action_type="archive_thread", source_event_id=ev_id)
    captured = _capture_publish(monkeypatch)

    def _boom(db, user, gmail_thread_id):
        raise RuntimeError("gmail blew up")

    monkeypatch.setattr("app.gmail.client.archive_thread", _boom)

    with pytest.raises(RuntimeError, match="gmail blew up"):
        action_tasks.execute_action.apply(args=[USER_ID, action_id])

    action = _get_action(session_factory, action_id)
    assert action.status == "failed"
    assert action.error == "gmail blew up"
    assert (USER_ID, "action_updated", {"task_id": TASK_ID}) in captured


# ---------------------------------------------------------------------------
# draft_reply empty body guard
# ---------------------------------------------------------------------------


def test_execute_action_inner_draft_reply_empty_body_fails_without_creating_draft(session_factory, fake_redis, monkeypatch):
    """Defensive: llm_client.call_messages returns "" on API errors instead of
    raising, so an empty body would silently flow into create_draft, producing
    a blank Gmail draft marked status='executed'. This test verifies the guard:
    empty LLM response -> RuntimeError -> no create_draft call."""
    _seed_user(session_factory, granted_scopes=_both_scopes())
    _seed_task(session_factory, goal="Land the deal")
    _seed_thread_and_message(session_factory)
    ev_id = _seed_event(session_factory, evidence_quote="the deal closed")
    action_id = _seed_action(
        session_factory, action_type="draft_reply", action_params={"instructions": "thank them"},
        source_event_id=ev_id,
    )

    async def _fake_empty_response(**kwargs):
        # Simulate LLM API error: returns empty string instead of raising.
        return ""

    monkeypatch.setattr(llm_client, "call_messages", _fake_empty_response)

    db = session_factory()
    action = actions_repo.get_owned_action(db, user_id=USER_ID, action_id=action_id)
    user = db.get(User, USER_ID)
    published = []

    gmail = MagicMock()
    with patch("app.gmail.client.get_gmail_client", return_value=gmail):
        with pytest.raises(RuntimeError, match="LLM returned an empty draft body"):
            action_tasks.execute_action_inner(
                db, user=user, action=action, publish=lambda *a: published.append(a),
            )
    db.close()

    # Verify the draft was never created — the guard prevents a blank draft.
    gmail.users().drafts().create.assert_not_called()
