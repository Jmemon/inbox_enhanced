"""Phase 5 (actions, spec 006) CRUD: task_action_rules and the task_actions
audit ledger they fire into.

Caller owns the transaction (Session) — this module never commits, matching
app.task_engine.repo / app.inbox.{bucket_repo,inbox_repo}. Write-path helpers
that INSERT a new row call db.flush() afterward so the generated id is
visible to the caller before commit.

Ids are uuid.uuid4().hex; timestamps are datetime.now(timezone.utc).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.actions.rules import ActionIntent
from app.db.models import Task, TaskAction, TaskActionRule

# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def create_rule(
    db: Session,
    *,
    task_id: str,
    trigger: str,
    trigger_params: dict | None,
    action_type: str,
    action_params: dict | None,
    mode: str,
) -> TaskActionRule:
    """Insert a new rule. is_deleted=False, created_at=now."""
    row = TaskActionRule(
        id=uuid.uuid4().hex,
        task_id=task_id,
        trigger=trigger,
        trigger_params=trigger_params,
        action_type=action_type,
        action_params=action_params,
        mode=mode,
        is_deleted=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def list_rules(db: Session, *, task_id: str, include_deleted: bool = False) -> list[TaskActionRule]:
    """This task's rules, created_at ascending. Soft-deleted rows are
    excluded unless include_deleted=True (mirrors bucket_repo/task_repo's
    is_deleted convention)."""
    stmt = select(TaskActionRule).where(TaskActionRule.task_id == task_id)
    if not include_deleted:
        stmt = stmt.where(TaskActionRule.is_deleted == False)  # noqa: E712
    stmt = stmt.order_by(TaskActionRule.created_at.asc())
    return list(db.execute(stmt).scalars().all())


def get_owned_rule(db: Session, *, user_id: str, rule_id: str) -> TaskActionRule | None:
    """Fetch one rule scoped to its owner via a join through Task (a rule has
    no user_id column of its own) — same no-enumeration-split rationale as
    task_engine.repo.get_owned_task: wrong-user, nonexistent, and
    soft-deleted-task rules all return None alike. Does NOT filter on the
    rule's own is_deleted — callers that need to distinguish "already
    deleted" from "not yours" (the idempotent-DELETE pattern,
    get_owned_task_any_status) can add that check themselves."""
    return db.execute(
        select(TaskActionRule)
        .join(Task, TaskActionRule.task_id == Task.id)
        .where(
            TaskActionRule.id == rule_id,
            Task.user_id == user_id,
            Task.is_deleted == False,  # noqa: E712
        )
    ).scalar_one_or_none()


def soft_delete_rule(db: Session, *, rule: TaskActionRule) -> None:
    """Mark a rule deleted in place — mirrors task_repo.soft_delete_task."""
    rule.is_deleted = True


def update_rule(db: Session, *, rule: TaskActionRule, **fields) -> TaskActionRule:
    """Apply field=value pairs onto an existing rule row in place (e.g.
    trigger_params=..., action_params=..., mode=...). Caller validates which
    fields are legal to change (e.g. the draft_reply-can't-auto-run rule) —
    this helper is a plain setattr loop, no row-level policy of its own."""
    for key, value in fields.items():
        setattr(rule, key, value)
    return rule


# ---------------------------------------------------------------------------
# Actions (the audit ledger)
# ---------------------------------------------------------------------------


def insert_intent(
    db: Session,
    *,
    task_id: str,
    intent: ActionIntent,
) -> TaskAction | None:
    """Insert one TaskAction row in status='proposed' from an evaluated
    ActionIntent (app.actions.rules). Idempotent under event replay/refold
    and link re-upsert: SELECT-first against whichever partial unique index
    applies (rule_id, source_event_id) or (rule_id, source_link_id), then an
    IntegrityError backstop around the actual insert for the race window
    between the SELECT and the flush — mirrors task_engine.transitions.
    _append_event_guarded + repo.find_event_for_message_field. Returns None
    (no row, no error) on either path finding a pre-existing row: a
    conflict here always means "already fired for this exact evidence,"
    never a real failure.
    """
    assert (intent.source_event_id is None) != (intent.source_link_id is None), (
        "ActionIntent must carry exactly one of source_event_id/source_link_id"
    )

    existing = _find_existing_action(
        db, rule_id=intent.rule_id,
        source_event_id=intent.source_event_id, source_link_id=intent.source_link_id,
    )
    if existing is not None:
        return None

    row = TaskAction(
        id=uuid.uuid4().hex,
        task_id=task_id,
        rule_id=intent.rule_id,
        source_event_id=intent.source_event_id,
        source_link_id=intent.source_link_id,
        thread_id=intent.thread_id,
        gmail_thread_id=intent.gmail_thread_id,
        action_type=intent.action_type,
        action_params=intent.action_params,
        status="proposed",
        created_at=datetime.now(timezone.utc),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None
    return row


def _find_existing_action(
    db: Session, *, rule_id: str, source_event_id: str | None, source_link_id: str | None,
) -> TaskAction | None:
    """SELECT-first idempotency check for insert_intent — the fast path
    ahead of the partial-unique-index race backstop. Exactly one of
    source_event_id/source_link_id is non-None (asserted by the caller), so
    exactly one of these two branches ever queries."""
    if source_event_id is not None:
        stmt = select(TaskAction).where(
            TaskAction.rule_id == rule_id, TaskAction.source_event_id == source_event_id,
        )
    else:
        stmt = select(TaskAction).where(
            TaskAction.rule_id == rule_id, TaskAction.source_link_id == source_link_id,
        )
    return db.execute(stmt).scalar_one_or_none()


def get_owned_action(db: Session, *, user_id: str, action_id: str) -> TaskAction | None:
    """Fetch one action scoped to its owner via a join through Task — same
    pattern as get_owned_rule (TaskAction has no user_id column either)."""
    return db.execute(
        select(TaskAction)
        .join(Task, TaskAction.task_id == Task.id)
        .where(
            TaskAction.id == action_id,
            Task.user_id == user_id,
            Task.is_deleted == False,  # noqa: E712
        )
    ).scalar_one_or_none()


def set_status(
    db: Session,
    *,
    action: TaskAction,
    status: str,
    result: dict | None = None,
    error: str | None = None,
    executed_at: datetime | None = None,
) -> None:
    """Transition an action's status in place. result/error/executed_at are
    only set when the caller passes them (approve/execute writes result +
    executed_at; a failure writes error; reject/undo just flip status) —
    omitted kwargs leave the existing column value untouched rather than
    clobbering it back to None."""
    action.status = status
    if result is not None:
        action.result = result
    if error is not None:
        action.error = error
    if executed_at is not None:
        action.executed_at = executed_at


# ---------------------------------------------------------------------------
# Aggregate feeds: cross-task reads for the review tray (proposed actions)
# and activity ticker (settled actions) — mirrors task_engine.repo's
# list_pending_events_for_user/list_recent_events_for_user exactly: scoping
# goes through the Task join (TaskAction has no user_id of its own), and
# Task.kind == 'tracker' is defense-in-depth (buckets can't have rules, so
# they structurally have no actions either, per design §6 invariant 9) —
# same posture as those two functions' own kind guard.
# ---------------------------------------------------------------------------


def list_pending_actions_for_user(
    db: Session, *, user_id: str, limit: int = 50,
) -> list[tuple[TaskAction, Task]]:
    """Every 'proposed' action across this user's non-deleted tracker tasks,
    newest first, paired with its owning task — the review-tray feed."""
    stmt = (
        select(TaskAction, Task)
        .join(Task, TaskAction.task_id == Task.id)
        .where(
            Task.user_id == user_id,
            Task.is_deleted == False,  # noqa: E712
            Task.kind == "tracker",
            TaskAction.status == "proposed",
        )
        .order_by(TaskAction.created_at.desc())
        .limit(limit)
    )
    return [(action, task) for action, task in db.execute(stmt).all()]


def list_recent_actions_for_user(
    db: Session, *, user_id: str, limit: int = 20,
) -> list[tuple[TaskAction, Task]]:
    """Every non-'proposed' action (executed/rejected/undone/failed) across
    this user's non-deleted tracker tasks, newest first, paired with its
    owning task — the activity-ticker feed. Companion query to
    list_pending_actions_for_user, same Task-join scoping."""
    stmt = (
        select(TaskAction, Task)
        .join(Task, TaskAction.task_id == Task.id)
        .where(
            Task.user_id == user_id,
            Task.is_deleted == False,  # noqa: E712
            Task.kind == "tracker",
            TaskAction.status != "proposed",
        )
        .order_by(TaskAction.created_at.desc())
        .limit(limit)
    )
    return [(action, task) for action, task in db.execute(stmt).all()]
