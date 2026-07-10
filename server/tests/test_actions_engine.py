"""Tests for app.actions.engine — the Phase 5 (actions, spec 006 Task 3)
rule-firing bridge between the pure evaluator (app.actions.rules) and the
audit-ledger CRUD (app.actions.repo).

Uses the shared `db` fixture (conftest.py, in-memory SQLite via
Base.metadata.create_all) with REAL ORM rows (Task/TaskEvent/TaskThreadLink)
— unlike test_action_rules.py's pure duck-typed fakes, this module does
real DB reads/writes (list_rules, insert_intent), so it needs the real
models. `publish` is passed in directly as a plain callable (no monkeypatch
needed — fire_rules_for_event/link take it as an explicit param); only the
late-imported `execute_action.apply_async` needs patching.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.actions import engine as actions_engine
from app.actions import repo as actions_repo
from app.db.models import Task, TaskEvent, TaskThreadLink, User

USER_ID = "user-1"
TASK_ID = "task-1"
THREAD_ID = "thread-1"
GMAIL_THREAD_ID = "gmail-thread-1"


def _seed_user(db, user_id=USER_ID) -> User:
    user = User(id=user_id, email=f"{user_id}@example.com", created_at=datetime.now(timezone.utc))
    db.add(user)
    db.flush()
    return user


def _seed_task(db, *, user_id=USER_ID, task_id=TASK_ID, kind="tracker") -> Task:
    task = Task(
        id=task_id, user_id=user_id, kind=kind, name="Task", goal="", criteria="",
        status="active", version=1, is_deleted=False, created_at=datetime.now(timezone.utc),
    )
    db.add(task)
    db.flush()
    return task


def _seed_rule(db, *, task_id=TASK_ID, trigger="entity_entered_stage", trigger_params=None,
                action_type="archive_thread", action_params=None, mode="propose"):
    if trigger_params is None and trigger == "entity_entered_stage":
        trigger_params = {"stage": "won"}
    return actions_repo.create_rule(
        db, task_id=task_id, trigger=trigger, trigger_params=trigger_params,
        action_type=action_type, action_params=action_params, mode=mode,
    )


def _applied_event(db, *, task: Task, field="stage", new_value="won") -> TaskEvent:
    event = TaskEvent(
        id=uuid.uuid4().hex, task_id=task.id, user_id=task.user_id, origin="llm", status="applied",
        field=field, new_value=new_value, thread_id=THREAD_ID, created_at=datetime.now(timezone.utc),
    )
    db.add(event)
    db.flush()
    return event


def _link(db, *, task: Task, thread_id=THREAD_ID) -> TaskThreadLink:
    now = datetime.now(timezone.utc)
    link = TaskThreadLink(
        id=uuid.uuid4().hex, task_id=task.id, thread_id=thread_id, user_id=task.user_id,
        origin="llm", state="attached", created_at=now, updated_at=now,
    )
    db.add(link)
    db.flush()
    return link


def _capture_publish():
    calls = []

    def _fake(user_id, event, payload):
        calls.append((user_id, event, payload))

    return _fake, calls


# ---------------------------------------------------------------------------
# fire_rules_for_event
# ---------------------------------------------------------------------------


def test_fire_rules_for_event_propose_mode_inserts_action_and_publishes(db):
    _seed_user(db)
    task = _seed_task(db)
    _seed_rule(db, mode="propose")
    event = _applied_event(db, task=task)
    publish, calls = _capture_publish()

    with patch("app.workers.action_tasks.execute_action.apply_async") as mock_apply:
        actions = actions_engine.fire_rules_for_event(
            db, user_id=USER_ID, task=task, event=event,
            thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
        )

    assert len(actions) == 1
    assert actions[0].status == "proposed"
    assert actions[0].source_event_id == event.id
    assert calls == [(USER_ID, "action_updated", {"task_id": task.id})]
    mock_apply.assert_not_called()


def test_fire_rules_for_event_auto_mode_enqueues_execute_action(db):
    _seed_user(db)
    task = _seed_task(db)
    _seed_rule(db, mode="auto", action_type="archive_thread")
    event = _applied_event(db, task=task)
    publish, _ = _capture_publish()

    with patch("app.workers.action_tasks.execute_action.apply_async") as mock_apply:
        actions = actions_engine.fire_rules_for_event(
            db, user_id=USER_ID, task=task, event=event,
            thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
        )

    assert len(actions) == 1
    mock_apply.assert_called_once_with(args=[USER_ID, actions[0].id])


def test_fire_rules_for_event_idempotent_on_replay(db):
    """Re-firing the SAME event (e.g. a replayed extraction) must not
    double-insert or double-dispatch — insert_intent's idempotency check
    returns None on the second call, which fire_rules_for_event/engine skips
    silently."""
    _seed_user(db)
    task = _seed_task(db)
    _seed_rule(db, mode="auto")
    event = _applied_event(db, task=task)
    publish, calls = _capture_publish()

    with patch("app.workers.action_tasks.execute_action.apply_async") as mock_apply:
        first = actions_engine.fire_rules_for_event(
            db, user_id=USER_ID, task=task, event=event,
            thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
        )
        second = actions_engine.fire_rules_for_event(
            db, user_id=USER_ID, task=task, event=event,
            thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
        )

    assert len(first) == 1
    assert second == []
    assert mock_apply.call_count == 1
    assert len(calls) == 1


def test_fire_rules_for_event_no_rules_is_a_no_op(db):
    _seed_user(db)
    task = _seed_task(db)
    event = _applied_event(db, task=task)
    publish, calls = _capture_publish()

    actions = actions_engine.fire_rules_for_event(
        db, user_id=USER_ID, task=task, event=event,
        thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
    )
    assert actions == []
    assert calls == []


def test_fire_rules_for_event_non_matching_rule_yields_no_intents(db):
    _seed_user(db)
    task = _seed_task(db)
    _seed_rule(db, trigger_params={"stage": "lost"})
    event = _applied_event(db, task=task, new_value="won")
    publish, calls = _capture_publish()

    actions = actions_engine.fire_rules_for_event(
        db, user_id=USER_ID, task=task, event=event,
        thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
    )
    assert actions == []
    assert calls == []


def test_fire_rules_for_event_bucket_kind_task_is_defensive_no_op(db):
    """Bucket-kind tasks structurally never get rules (spec §2) — this is
    defense-in-depth against a stray call, e.g. if a caller's own kind guard
    were ever bypassed."""
    _seed_user(db)
    task = _seed_task(db, task_id="bucket-1", kind="bucket")
    _seed_rule(db, task_id=task.id)
    event = _applied_event(db, task=task)
    publish, calls = _capture_publish()

    actions = actions_engine.fire_rules_for_event(
        db, user_id=USER_ID, task=task, event=event,
        thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
    )
    assert actions == []
    assert calls == []


def test_fire_rules_for_event_auto_draft_reply_asserts(db):
    """Belt + suspenders (spec §6 invariant 2): a draft_reply rule must never
    be mode='auto' — the rule-write API (T4) is the first line of defense;
    this guard inside the engine's dispatch is the second, independent of
    whatever validation a caller may or may not have applied."""
    _seed_user(db)
    task = _seed_task(db)
    _seed_rule(db, mode="auto", action_type="draft_reply", action_params={"instructions": "hi"})
    event = _applied_event(db, task=task)
    publish, _ = _capture_publish()

    with pytest.raises(RuntimeError, match="draft_reply actions can never auto-execute"):
        actions_engine.fire_rules_for_event(
            db, user_id=USER_ID, task=task, event=event,
            thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
        )


# ---------------------------------------------------------------------------
# fire_rules_for_link
# ---------------------------------------------------------------------------


def test_fire_rules_for_link_propose_mode_inserts_action_and_publishes(db):
    _seed_user(db)
    task = _seed_task(db)
    _seed_rule(db, trigger="thread_linked", trigger_params=None, action_type="label_thread",
               action_params={"label": "Tracked"}, mode="propose")
    link = _link(db, task=task)
    publish, calls = _capture_publish()

    with patch("app.workers.action_tasks.execute_action.apply_async") as mock_apply:
        actions = actions_engine.fire_rules_for_link(
            db, user_id=USER_ID, task=task, link=link,
            thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
        )

    assert len(actions) == 1
    assert actions[0].source_link_id == link.id
    assert calls == [(USER_ID, "action_updated", {"task_id": task.id})]
    mock_apply.assert_not_called()


def test_fire_rules_for_link_auto_mode_enqueues_execute_action(db):
    _seed_user(db)
    task = _seed_task(db)
    _seed_rule(db, trigger="thread_linked", trigger_params=None, action_type="archive_thread",
               mode="auto")
    link = _link(db, task=task)
    publish, _ = _capture_publish()

    with patch("app.workers.action_tasks.execute_action.apply_async") as mock_apply:
        actions = actions_engine.fire_rules_for_link(
            db, user_id=USER_ID, task=task, link=link,
            thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
        )

    assert len(actions) == 1
    mock_apply.assert_called_once_with(args=[USER_ID, actions[0].id])


def test_fire_rules_for_link_idempotent_on_replay(db):
    _seed_user(db)
    task = _seed_task(db)
    _seed_rule(db, trigger="thread_linked", trigger_params=None, mode="auto")
    link = _link(db, task=task)
    publish, calls = _capture_publish()

    with patch("app.workers.action_tasks.execute_action.apply_async") as mock_apply:
        first = actions_engine.fire_rules_for_link(
            db, user_id=USER_ID, task=task, link=link,
            thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
        )
        second = actions_engine.fire_rules_for_link(
            db, user_id=USER_ID, task=task, link=link,
            thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
        )

    assert len(first) == 1
    assert second == []
    assert mock_apply.call_count == 1
    assert len(calls) == 1


def test_fire_rules_for_link_no_rules_is_a_no_op(db):
    _seed_user(db)
    task = _seed_task(db)
    link = _link(db, task=task)
    publish, calls = _capture_publish()

    actions = actions_engine.fire_rules_for_link(
        db, user_id=USER_ID, task=task, link=link,
        thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
    )
    assert actions == []
    assert calls == []


def test_fire_rules_for_link_bucket_kind_task_is_defensive_no_op(db):
    _seed_user(db)
    task = _seed_task(db, task_id="bucket-1", kind="bucket")
    _seed_rule(db, task_id=task.id, trigger="thread_linked", trigger_params=None)
    link = _link(db, task=task)
    publish, calls = _capture_publish()

    actions = actions_engine.fire_rules_for_link(
        db, user_id=USER_ID, task=task, link=link,
        thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
    )
    assert actions == []
    assert calls == []


def test_fire_rules_for_link_multiple_rules_each_get_their_own_action(db):
    _seed_user(db)
    task = _seed_task(db)
    _seed_rule(db, trigger="thread_linked", trigger_params=None, action_type="archive_thread", mode="propose")
    _seed_rule(db, trigger="thread_linked", trigger_params=None, action_type="label_thread",
               action_params={"label": "x"}, mode="propose")
    link = _link(db, task=task)
    publish, calls = _capture_publish()

    actions = actions_engine.fire_rules_for_link(
        db, user_id=USER_ID, task=task, link=link,
        thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID, publish=publish,
    )
    assert len(actions) == 2
    assert len(calls) == 2
