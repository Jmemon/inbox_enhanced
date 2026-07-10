"""Tests for app.actions.repo — the Phase 5 (actions, spec 006) rules +
audit-ledger CRUD. Uses the shared `db` fixture (conftest.py): an in-memory
SQLite engine built from Base.metadata.create_all, so the CHECK constraint
and both partial unique indexes declared on TaskAction (app/db/models.py)
are live here too, not just under the Alembic-migrated engine that
test_migration_0011.py exercises.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.actions import repo as actions_repo
from app.actions.rules import ActionIntent
from app.db.models import Task, TaskEvent, TaskThreadLink, User

USER_ID = "user-1"
OTHER_USER_ID = "user-2"
TASK_ID = "task-1"
THREAD_ID = "thread-1"
GMAIL_THREAD_ID = "gmail-thread-1"


def _seed_user(db, user_id=USER_ID) -> User:
    user = User(id=user_id, email=f"{user_id}@example.com", created_at=datetime.now(timezone.utc))
    db.add(user)
    db.flush()
    return user


def _seed_task(db, *, user_id=USER_ID, task_id=TASK_ID, kind="tracker", is_deleted=False) -> Task:
    task = Task(
        id=task_id, user_id=user_id, kind=kind, name="Task", goal="", criteria="",
        status="active", version=1, is_deleted=is_deleted, created_at=datetime.now(timezone.utc),
    )
    db.add(task)
    db.flush()
    return task


def _seed_event(db, *, task: Task, field="stage", new_value="won", status="applied") -> TaskEvent:
    event = TaskEvent(
        id=uuid.uuid4().hex, task_id=task.id, user_id=task.user_id, origin="llm", status=status,
        field=field, new_value=new_value, created_at=datetime.now(timezone.utc),
    )
    db.add(event)
    db.flush()
    return event


def _seed_link(db, *, task: Task, thread_id=THREAD_ID) -> TaskThreadLink:
    now = datetime.now(timezone.utc)
    link = TaskThreadLink(
        id=uuid.uuid4().hex, task_id=task.id, thread_id=thread_id, user_id=task.user_id,
        origin="llm", state="attached", created_at=now, updated_at=now,
    )
    db.add(link)
    db.flush()
    return link


def _seed_rule(db, *, task_id=TASK_ID, trigger="entity_entered_stage", trigger_params=None,
                action_type="archive_thread", action_params=None, mode="propose"):
    if trigger_params is None and trigger == "entity_entered_stage":
        trigger_params = {"stage": "won"}
    return actions_repo.create_rule(
        db, task_id=task_id, trigger=trigger, trigger_params=trigger_params,
        action_type=action_type, action_params=action_params, mode=mode,
    )


def _event_intent(*, rule_id: str, event_id: str, thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID,
                   action_type="archive_thread", action_params=None) -> ActionIntent:
    return ActionIntent(
        rule_id=rule_id, action_type=action_type, action_params=action_params,
        source_event_id=event_id, source_link_id=None,
        thread_id=thread_id, gmail_thread_id=gmail_thread_id,
    )


def _link_intent(*, rule_id: str, link_id: str, thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID,
                  action_type="label_thread", action_params=None) -> ActionIntent:
    return ActionIntent(
        rule_id=rule_id, action_type=action_type, action_params=action_params,
        source_event_id=None, source_link_id=link_id,
        thread_id=thread_id, gmail_thread_id=gmail_thread_id,
    )


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def test_create_rule_sets_defaults(db):
    _seed_user(db)
    _seed_task(db)
    rule = _seed_rule(db)
    assert rule.id
    assert rule.is_deleted is False
    assert rule.created_at is not None
    assert rule.mode == "propose"


def test_list_rules_excludes_deleted_by_default(db):
    _seed_user(db)
    _seed_task(db)
    keep = _seed_rule(db)
    gone = _seed_rule(db, action_type="label_thread", action_params={"label": "x"})
    actions_repo.soft_delete_rule(db, rule=gone)
    db.flush()

    active = actions_repo.list_rules(db, task_id=TASK_ID)
    assert [r.id for r in active] == [keep.id]

    everything = actions_repo.list_rules(db, task_id=TASK_ID, include_deleted=True)
    assert {r.id for r in everything} == {keep.id, gone.id}


def test_get_owned_rule_cross_user_returns_none(db):
    _seed_user(db, user_id=USER_ID)
    _seed_user(db, user_id=OTHER_USER_ID)
    _seed_task(db, user_id=USER_ID)
    rule = _seed_rule(db)

    assert actions_repo.get_owned_rule(db, user_id=USER_ID, rule_id=rule.id) is not None
    assert actions_repo.get_owned_rule(db, user_id=OTHER_USER_ID, rule_id=rule.id) is None


def test_get_owned_rule_hidden_once_owning_task_is_deleted(db):
    _seed_user(db)
    _seed_task(db)
    rule = _seed_rule(db)

    task = db.get(Task, TASK_ID)
    task.is_deleted = True
    db.flush()

    assert actions_repo.get_owned_rule(db, user_id=USER_ID, rule_id=rule.id) is None


def test_soft_delete_rule_marks_deleted_in_place(db):
    _seed_user(db)
    _seed_task(db)
    rule = _seed_rule(db)
    actions_repo.soft_delete_rule(db, rule=rule)
    assert rule.is_deleted is True


def test_update_rule_mutates_only_given_fields(db):
    _seed_user(db)
    _seed_task(db)
    rule = _seed_rule(db, mode="propose")
    actions_repo.update_rule(db, rule=rule, mode="auto", action_params={"note": "x"})
    assert rule.mode == "auto"
    assert rule.action_params == {"note": "x"}
    assert rule.trigger == "entity_entered_stage"  # untouched


# ---------------------------------------------------------------------------
# insert_intent
# ---------------------------------------------------------------------------


def test_insert_intent_creates_proposed_row_with_frozen_params(db):
    _seed_user(db)
    task = _seed_task(db)
    rule = _seed_rule(db, action_type="label_thread", action_params={"label": "Won"})
    event = _seed_event(db, task=task)

    intent = _event_intent(
        rule_id=rule.id, event_id=event.id, action_type="label_thread", action_params={"label": "Won"},
    )
    row = actions_repo.insert_intent(db, task_id=task.id, intent=intent)

    assert row is not None
    assert row.status == "proposed"
    assert row.rule_id == rule.id
    assert row.source_event_id == event.id
    assert row.source_link_id is None
    assert row.action_type == "label_thread"
    assert row.action_params == {"label": "Won"}
    assert row.gmail_thread_id == GMAIL_THREAD_ID
    assert row.created_at is not None


def test_insert_intent_from_a_link_source(db):
    _seed_user(db)
    task = _seed_task(db)
    rule = _seed_rule(db, trigger="thread_linked", trigger_params=None, action_type="archive_thread")
    link = _seed_link(db, task=task)

    intent = _link_intent(rule_id=rule.id, link_id=link.id, action_type="archive_thread")
    row = actions_repo.insert_intent(db, task_id=task.id, intent=intent)

    assert row is not None
    assert row.source_event_id is None
    assert row.source_link_id == link.id


def test_insert_intent_asserts_exactly_one_source(db):
    """Defense in depth: even if a caller hand-builds an ActionIntent that
    violates the CHECK constraint's invariant (bypassing rules.py's own
    construction, which never does this), insert_intent refuses to even
    attempt the insert."""
    _seed_user(db)
    task = _seed_task(db)
    rule = _seed_rule(db)

    both_set = ActionIntent(
        rule_id=rule.id, action_type="archive_thread", action_params=None,
        source_event_id="evt-x", source_link_id="link-x",
        thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID,
    )
    with pytest.raises(AssertionError):
        actions_repo.insert_intent(db, task_id=task.id, intent=both_set)

    neither_set = ActionIntent(
        rule_id=rule.id, action_type="archive_thread", action_params=None,
        source_event_id=None, source_link_id=None,
        thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID,
    )
    with pytest.raises(AssertionError):
        actions_repo.insert_intent(db, task_id=task.id, intent=neither_set)


def test_insert_intent_idempotent_on_duplicate_rule_event_pair(db):
    _seed_user(db)
    task = _seed_task(db)
    rule = _seed_rule(db)
    event = _seed_event(db, task=task)
    intent = _event_intent(rule_id=rule.id, event_id=event.id)

    first = actions_repo.insert_intent(db, task_id=task.id, intent=intent)
    second = actions_repo.insert_intent(db, task_id=task.id, intent=intent)

    assert first is not None
    assert second is None


def test_insert_intent_idempotent_on_duplicate_rule_link_pair(db):
    _seed_user(db)
    task = _seed_task(db)
    rule = _seed_rule(db, trigger="thread_linked", trigger_params=None)
    link = _seed_link(db, task=task)
    intent = _link_intent(rule_id=rule.id, link_id=link.id)

    first = actions_repo.insert_intent(db, task_id=task.id, intent=intent)
    second = actions_repo.insert_intent(db, task_id=task.id, intent=intent)

    assert first is not None
    assert second is None


def test_insert_intent_same_event_different_rule_is_not_blocked(db):
    _seed_user(db)
    task = _seed_task(db)
    rule_a = _seed_rule(db)
    rule_b = _seed_rule(db, action_type="label_thread", action_params={"label": "x"})
    event = _seed_event(db, task=task)

    first = actions_repo.insert_intent(db, task_id=task.id, intent=_event_intent(rule_id=rule_a.id, event_id=event.id))
    second = actions_repo.insert_intent(db, task_id=task.id, intent=_event_intent(rule_id=rule_b.id, event_id=event.id))

    assert first is not None
    assert second is not None


# ---------------------------------------------------------------------------
# get_owned_action / set_status
# ---------------------------------------------------------------------------


def test_get_owned_action_cross_user_returns_none(db):
    _seed_user(db, user_id=USER_ID)
    _seed_user(db, user_id=OTHER_USER_ID)
    task = _seed_task(db, user_id=USER_ID)
    rule = _seed_rule(db)
    event = _seed_event(db, task=task)
    action = actions_repo.insert_intent(db, task_id=task.id, intent=_event_intent(rule_id=rule.id, event_id=event.id))

    assert actions_repo.get_owned_action(db, user_id=USER_ID, action_id=action.id) is not None
    assert actions_repo.get_owned_action(db, user_id=OTHER_USER_ID, action_id=action.id) is None


def test_set_status_updates_only_given_fields(db):
    _seed_user(db)
    task = _seed_task(db)
    rule = _seed_rule(db)
    event = _seed_event(db, task=task)
    action = actions_repo.insert_intent(db, task_id=task.id, intent=_event_intent(rule_id=rule.id, event_id=event.id))

    now = datetime.now(timezone.utc)
    actions_repo.set_status(db, action=action, status="executed", result={"removed_label_ids": ["INBOX"]}, executed_at=now)
    assert action.status == "executed"
    assert action.result == {"removed_label_ids": ["INBOX"]}
    assert action.executed_at == now
    assert action.error is None

    actions_repo.set_status(db, action=action, status="failed", error="needs permission")
    assert action.status == "failed"
    assert action.error == "needs permission"
    # Previously-set result is left untouched by a call that doesn't pass one.
    assert action.result == {"removed_label_ids": ["INBOX"]}


# ---------------------------------------------------------------------------
# Aggregate feeds
# ---------------------------------------------------------------------------


def test_list_pending_actions_for_user_scoped_ordered_and_excludes_settled(db):
    _seed_user(db, user_id=USER_ID)
    _seed_user(db, user_id=OTHER_USER_ID)
    task = _seed_task(db, user_id=USER_ID)
    other_task = _seed_task(db, user_id=OTHER_USER_ID, task_id="task-other")
    rule = _seed_rule(db)
    other_rule = _seed_rule(db, task_id=other_task.id)

    ev1 = _seed_event(db, task=task)
    ev2 = _seed_event(db, task=task)
    other_ev = _seed_event(db, task=other_task)

    proposed_1 = actions_repo.insert_intent(db, task_id=task.id, intent=_event_intent(rule_id=rule.id, event_id=ev1.id))
    proposed_2 = actions_repo.insert_intent(db, task_id=task.id, intent=_event_intent(rule_id=rule.id, event_id=ev2.id))
    settled = actions_repo.insert_intent(db, task_id=task.id, intent=_event_intent(rule_id=rule.id, event_id="evt-settled"))
    actions_repo.set_status(db, action=settled, status="executed", result={}, executed_at=datetime.now(timezone.utc))
    actions_repo.insert_intent(db, task_id=other_task.id, intent=_event_intent(rule_id=other_rule.id, event_id=other_ev.id))
    db.flush()  # set_status mutates in place; autoflush=False (see repo module docstring) — flush before querying

    pending = actions_repo.list_pending_actions_for_user(db, user_id=USER_ID)
    pending_ids = [action.id for action, _ in pending]

    assert set(pending_ids) == {proposed_1.id, proposed_2.id}
    assert settled.id not in pending_ids
    # newest first
    assert pending_ids[0] == proposed_2.id
    assert all(t.id == task.id for _, t in pending)


def test_list_recent_actions_for_user_excludes_proposed(db):
    _seed_user(db)
    task = _seed_task(db)
    rule = _seed_rule(db)
    ev1 = _seed_event(db, task=task)
    ev2 = _seed_event(db, task=task)

    still_proposed = actions_repo.insert_intent(db, task_id=task.id, intent=_event_intent(rule_id=rule.id, event_id=ev1.id))
    executed = actions_repo.insert_intent(db, task_id=task.id, intent=_event_intent(rule_id=rule.id, event_id=ev2.id))
    actions_repo.set_status(db, action=executed, status="executed", result={}, executed_at=datetime.now(timezone.utc))
    db.flush()  # set_status mutates in place; autoflush=False (see repo module docstring) — flush before querying

    recent = actions_repo.list_recent_actions_for_user(db, user_id=USER_ID)
    recent_ids = [action.id for action, _ in recent]

    assert recent_ids == [executed.id]
    assert still_proposed.id not in recent_ids


def test_feeds_exclude_bucket_kind_tasks(db):
    """Defense-in-depth: buckets can't have rules per the design (§6
    invariant 9), but if a TaskAction somehow existed under a bucket-kind
    task, both feeds still must not surface it — mirrors task_engine.repo's
    own Task.kind == 'tracker' guard on its feed queries."""
    _seed_user(db)
    bucket_task = _seed_task(db, task_id="bucket-1", kind="bucket")
    rule = _seed_rule(db, task_id=bucket_task.id)
    event = _seed_event(db, task=bucket_task)
    actions_repo.insert_intent(db, task_id=bucket_task.id, intent=_event_intent(rule_id=rule.id, event_id=event.id))

    assert actions_repo.list_pending_actions_for_user(db, user_id=USER_ID) == []
    assert actions_repo.list_recent_actions_for_user(db, user_id=USER_ID) == []


def test_feeds_exclude_soft_deleted_tasks(db):
    _seed_user(db)
    task = _seed_task(db)
    rule = _seed_rule(db)
    event = _seed_event(db, task=task)
    actions_repo.insert_intent(db, task_id=task.id, intent=_event_intent(rule_id=rule.id, event_id=event.id))

    task.is_deleted = True
    db.flush()

    assert actions_repo.list_pending_actions_for_user(db, user_id=USER_ID) == []
