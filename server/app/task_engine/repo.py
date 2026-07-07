"""Task-engine CRUD: tasks, thread links, state entities, and the append-only
event log they fold from.

Caller owns the transaction (Session) — this module never commits, matching
app.inbox.{bucket_repo,inbox_repo}. Write-path helpers that INSERT a new row
call db.flush() afterward so the generated id is visible to the caller before
commit; pure-read helpers never flush (sessions here run with
autoflush=False, see app/db/session.py — callers that mutated an
already-loaded ORM object and need that write visible to a subsequent SELECT
in this module are responsible for flushing first, same convention as
inbox_repo.recompute_thread_pointers / list_threads).

Ids are uuid.uuid4().hex; timestamps are datetime.now(timezone.utc).

Two invariants worth naming up front because nothing else in this file
enforces them for you:

- upsert_link's sticky rule: an existing origin='user' link can never be
  silently downgraded by an origin='llm' upsert (reclassify runs are LLM
  origin and must not clobber a human's explicit attach/detach decision).
  Every other origin combination inserts or updates freely.
- TaskStateEntity.state is a derived cache, never a primary record — the
  primary record is the TaskEvent log. refold_entity() is the fold that
  rebuilds it, and is the substrate every revert/reject/detach/merge
  operation is built on (Task 6+): flip the relevant event(s)' status away
  from 'applied' and call refold_entity to make entity.state catch up.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import Task, TaskEvent, TaskStateEntity, TaskThreadLink

# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def create_task(
    db: Session,
    *,
    user_id: str,
    name: str,
    goal: str,
    criteria: str,
    state_schema: dict | None,
    kind: str = "tracker",
) -> Task:
    """Insert a new task. status='active', version=1, is_deleted=False."""
    row = Task(
        id=uuid.uuid4().hex,
        user_id=user_id,
        kind=kind,
        name=name,
        goal=goal,
        criteria=criteria,
        state_schema=state_schema,
        status="active",
        version=1,
        is_deleted=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def get_owned_task(db: Session, *, user_id: str, task_id: str) -> Task | None:
    """Fetch one task scoped to its owner. Returns None for wrong-user,
    nonexistent, or soft-deleted tasks alike (no enumeration split), mirroring
    inbox_repo.get_message."""
    return db.execute(
        select(Task).where(
            Task.id == task_id,
            Task.user_id == user_id,
            Task.is_deleted == False,  # noqa: E712
        )
    ).scalar_one_or_none()


def list_tasks(db: Session, *, user_id: str, kind: str | None = None) -> list[Task]:
    """This user's non-deleted active+paused tasks, name ascending. Optionally
    narrowed to one `kind` ('tracker' | 'bucket')."""
    stmt = (
        select(Task)
        .where(
            Task.user_id == user_id,
            Task.is_deleted == False,  # noqa: E712
            Task.status.in_(("active", "paused")),
        )
        .order_by(Task.name.asc())
    )
    if kind is not None:
        stmt = stmt.where(Task.kind == kind)
    return list(db.execute(stmt).scalars().all())


def list_active_trackers(db: Session, *, user_id: str) -> list[Task]:
    """Active, non-deleted, schema-bearing tracker tasks — the set the
    extraction pipeline (Task 6+) runs against. A tracker with no
    state_schema yet (schema still being proposed) is excluded.

    Ordered by (created_at, id) ascending for deterministic iteration —
    `process_task_updates` iterates this list and now isolates a per-task
    failure (Task 7 review fix), but the run-to-run order those failures are
    encountered in should still be stable rather than left to the database's
    unspecified default ordering.
    """
    stmt = (
        select(Task)
        .where(
            Task.user_id == user_id,
            Task.is_deleted == False,  # noqa: E712
            Task.kind == "tracker",
            Task.status == "active",
            Task.state_schema.is_not(None),
        )
        .order_by(Task.created_at.asc(), Task.id.asc())
    )
    return list(db.execute(stmt).scalars().all())


def bump_version(db: Session, *, task: Task) -> int:
    """Increment task.version (SSE gap-detection counter, D4) and return it."""
    task.version += 1
    return task.version


def get_owned_task_any_status(db: Session, *, user_id: str, task_id: str) -> Task | None:
    """Fetch a task scoped to its owner WITHOUT the is_deleted filter
    get_owned_task applies. Added for Task 10's idempotent DELETE endpoint:
    get_owned_task alone can't distinguish "already soft-deleted by you"
    (should 204 no-op) from "not yours / never existed" (should 404) since
    both return None from that query. This variant still scopes to
    (task_id, user_id) — a wrong-user id returns None here too, so it never
    leaks existence across users."""
    return db.execute(
        select(Task).where(Task.id == task_id, Task.user_id == user_id)
    ).scalar_one_or_none()


def soft_delete_task(db: Session, *, task: Task) -> None:
    """Mark a task deleted in place — mirrors bucket_repo.soft_delete."""
    task.is_deleted = True


# ---------------------------------------------------------------------------
# Thread links
# ---------------------------------------------------------------------------


def upsert_link(
    db: Session,
    *,
    task_id: str,
    thread_id: str,
    user_id: str,
    origin: str,
    state: str = "attached",
    confidence: int | None = None,
) -> TaskThreadLink | None:
    """Insert-or-update the (task_id, thread_id) link (uq_task_thread).

    THE sticky rule: if a row already exists with origin='user' and this call
    passes origin='llm', the call is a no-op — returns None and changes
    nothing. This is what lets a user's explicit attach/detach survive a
    later automatic reclassify. Every other combination (no existing row;
    existing row is origin='llm'; this call is origin='user' regardless of
    the existing row's origin) inserts or updates state/confidence/origin/
    updated_at and returns the row.
    """
    row = db.execute(
        select(TaskThreadLink).where(
            TaskThreadLink.task_id == task_id,
            TaskThreadLink.thread_id == thread_id,
        )
    ).scalar_one_or_none()

    if row is not None and row.origin == "user" and origin == "llm":
        return None

    now = datetime.now(timezone.utc)
    if row is None:
        row = TaskThreadLink(
            id=uuid.uuid4().hex,
            task_id=task_id,
            thread_id=thread_id,
            user_id=user_id,
            origin=origin,
            state=state,
            confidence=confidence,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
    else:
        row.origin = origin
        row.state = state
        row.confidence = confidence
        row.updated_at = now
    return row


def list_attached_thread_ids(db: Session, *, task_id: str) -> set[str]:
    """thread_ids currently in state='attached' for this task (detached links
    are excluded — they're history, not membership)."""
    stmt = select(TaskThreadLink.thread_id).where(
        TaskThreadLink.task_id == task_id,
        TaskThreadLink.state == "attached",
    )
    return set(db.execute(stmt).scalars().all())


def get_link(db: Session, *, task_id: str, thread_id: str) -> TaskThreadLink | None:
    """Bare (task_id, thread_id) lookup, regardless of state."""
    return db.execute(
        select(TaskThreadLink).where(
            TaskThreadLink.task_id == task_id,
            TaskThreadLink.thread_id == thread_id,
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# State entities
# ---------------------------------------------------------------------------


def get_or_create_entity(
    db: Session, *, task_id: str, user_id: str, entity_key: str, display_name: str
) -> TaskStateEntity:
    """Fetch the (task_id, entity_key) entity (uq_task_entity) or create it
    with empty state. entity_key is the caller-normalized key ('_self' for
    singleton tasks)."""
    row = db.execute(
        select(TaskStateEntity).where(
            TaskStateEntity.task_id == task_id,
            TaskStateEntity.entity_key == entity_key,
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = TaskStateEntity(
        id=uuid.uuid4().hex,
        task_id=task_id,
        user_id=user_id,
        entity_key=entity_key,
        display_name=display_name,
        state={},
        updated_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def list_entities(db: Session, *, task_id: str) -> list[TaskStateEntity]:
    """The board: this task's entities, most-recently-updated first."""
    stmt = (
        select(TaskStateEntity)
        .where(TaskStateEntity.task_id == task_id)
        .order_by(TaskStateEntity.updated_at.desc())
    )
    return list(db.execute(stmt).scalars().all())


def get_entity(db: Session, *, task_id: str, entity_id: str) -> TaskStateEntity | None:
    """Bare (task_id, entity_id) lookup, scoped to the task — for a caller
    (Task 10's API router) that has already confirmed task ownership via
    get_owned_task, this is the same one-level-down scoping trick: a
    mismatched task_id returns None rather than another task's entity."""
    return db.execute(
        select(TaskStateEntity).where(
            TaskStateEntity.id == entity_id, TaskStateEntity.task_id == task_id,
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def append_event(
    db: Session,
    *,
    task: Task,
    entity: TaskStateEntity | None,
    origin: str,
    status: str,
    field: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    evidence_quote: str | None = None,
    confidence: int | None = None,
    thread_id: str | None = None,
    message_id: str | None = None,
    gmail_message_id: str | None = None,
    pending_reason: str | None = None,
    proposed_entity: str | None = None,
) -> TaskEvent:
    """Append one row to the audit log. Flushes so event.id materializes
    (callers key later revert/reject calls off it) — does NOT apply the
    change to entity.state; call apply_event separately for that.

    pending_reason/proposed_entity are the pending-provenance fields written
    by transitions.py's guard chain (spec §4.4) — optional and None for
    every non-pending caller (applied events, api/tasks.py's manual state
    edit, etc.)."""
    row = TaskEvent(
        id=uuid.uuid4().hex,
        task_id=task.id,
        user_id=task.user_id,
        entity_id=entity.id if entity is not None else None,
        thread_id=thread_id,
        message_id=message_id,
        gmail_message_id=gmail_message_id,
        field=field,
        old_value=old_value,
        new_value=new_value,
        evidence_quote=evidence_quote,
        confidence=confidence,
        origin=origin,
        status=status,
        pending_reason=pending_reason,
        proposed_entity=proposed_entity,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def apply_event(db: Session, *, task: Task, entity: TaskStateEntity, event: TaskEvent) -> None:
    """Mark event applied and fold its field/new_value into entity.state.

    Reassigns entity.state to a new dict (rather than mutating the existing
    one in place) so SQLAlchemy's change-tracking on the JSON column actually
    sees the write. Bumps task.version (SSE gap detection, D4)."""
    event.status = "applied"
    new_state = dict(entity.state)
    new_state[event.field] = event.new_value
    entity.state = new_state
    entity.updated_at = datetime.now(timezone.utc)
    bump_version(db, task=task)


def list_events(
    db: Session,
    *,
    task_id: str,
    status: str | None = None,
    entity_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[TaskEvent]:
    """This task's events, newest first. Optional status/entity_id filters
    for the review queue and the per-entity history view."""
    stmt = (
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(TaskEvent.status == status)
    if entity_id is not None:
        stmt = stmt.where(TaskEvent.entity_id == entity_id)
    return list(db.execute(stmt).scalars().all())


def get_event(db: Session, *, task_id: str, event_id: str) -> TaskEvent | None:
    """Bare (task_id, event_id) lookup — same task-scoping rationale as
    get_entity, used by the approve/reject/revert correction endpoints."""
    return db.execute(
        select(TaskEvent).where(
            TaskEvent.id == event_id, TaskEvent.task_id == task_id,
        )
    ).scalar_one_or_none()


def list_applied_events_for_thread(db: Session, *, task_id: str, thread_id: str) -> list[TaskEvent]:
    """Every currently-'applied' event this task recorded with provenance
    pointing at one thread — the substrate for the user-detach correction
    (Task 10): DELETE /api/tasks/{id}/threads/{thread_id} flips each of these
    to 'reverted', then refolds every entity_id they touched so the board
    catches up in the same request."""
    stmt = select(TaskEvent).where(
        TaskEvent.task_id == task_id,
        TaskEvent.thread_id == thread_id,
        TaskEvent.status == "applied",
    )
    return list(db.execute(stmt).scalars().all())


def list_pending_events_for_thread(db: Session, *, task_id: str, thread_id: str) -> list[TaskEvent]:
    """Every currently-'pending_review' event this task recorded with
    provenance pointing at one thread — the companion query to
    list_applied_events_for_thread, for the same user-detach correction: a
    detached thread's not-yet-reviewed proposals must not remain approvable,
    even though (unlike applied events) there's no entity.state to refold
    since a pending event was never folded in."""
    stmt = select(TaskEvent).where(
        TaskEvent.task_id == task_id,
        TaskEvent.thread_id == thread_id,
        TaskEvent.status == "pending_review",
    )
    return list(db.execute(stmt).scalars().all())


def pending_count(db: Session, *, task_id: str) -> int:
    """Count of this task's events awaiting human review (status =
    'pending_review') — drives the review-queue badge."""
    stmt = select(func.count()).select_from(TaskEvent).where(
        TaskEvent.task_id == task_id,
        TaskEvent.status == "pending_review",
    )
    return db.execute(stmt).scalar_one()


def refold_entity(db: Session, *, task: Task, entity: TaskStateEntity) -> None:
    """Rebuild entity.state from scratch as a fold over this entity's
    APPLIED events, ascending by (created_at, origin) where 'llm' sorts
    before 'user' — so a user-origin event with the same created_at as an
    llm-origin event is folded in last and wins the tie. Fields with no
    surviving applied event (e.g. its one applied event got reverted/
    rejected) are removed from state entirely; this is the revert / detach /
    merge substrate — those operations flip an event's status away from
    'applied' (or reassign entity_id, for merge) and then call this to make
    entity.state catch up. Bumps task.version (SSE gap detection, D4).
    """
    stmt = (
        select(TaskEvent)
        .where(TaskEvent.entity_id == entity.id, TaskEvent.status == "applied")
    )
    events = list(db.execute(stmt).scalars().all())
    events.sort(key=lambda e: (e.created_at, 0 if e.origin == "llm" else 1))

    new_state: dict = {}
    for event in events:
        new_state[event.field] = event.new_value
    # 'stage' is the one reserved field the board always expects to find
    # (even if unset) — every other field simply stays absent if nothing
    # applied ever set it.
    new_state.setdefault("stage", None)

    entity.state = new_state
    entity.updated_at = datetime.now(timezone.utc)
    bump_version(db, task=task)


def repoint_entity_events(db: Session, *, task_id: str, from_entity_id: str, to_entity_id: str) -> None:
    """Merge substrate (Task 10): reassign every event on the losing entity
    to the winner's id, regardless of status — applied/pending_review/
    rejected/reverted all carry provenance that must survive a merge; only
    the entity_id pointer moves. Caller is responsible for refolding
    `to_entity_id` afterward (so its `state` catches up with the
    newly-adopted applied events) and then deleting the now-orphaned loser
    row via delete_entity()."""
    db.execute(
        update(TaskEvent)
        .where(TaskEvent.task_id == task_id, TaskEvent.entity_id == from_entity_id)
        .values(entity_id=to_entity_id)
    )


def delete_entity(db: Session, *, entity: TaskStateEntity) -> None:
    """Hard-delete a TaskStateEntity row. Only ever called on a merge's loser
    AFTER repoint_entity_events has already moved its events off of it —
    TaskEvent.entity_id is a soft pointer with no FK (see db/models.py), so
    this never orphans a foreign-key reference."""
    db.delete(entity)


# ---------------------------------------------------------------------------
# Extraction validator support (Task 6, task_engine.transitions)
# ---------------------------------------------------------------------------


def latest_applied_user_event(db: Session, *, entity_id: str) -> TaskEvent | None:
    """Most recent (by created_at) origin='user' status='applied' event for
    this entity, across all fields — the extraction validator's correction
    fence (spec §4.4 step 5): a proposal may only move this entity if its
    evidence message is strictly newer than this event."""
    stmt = (
        select(TaskEvent)
        .where(
            TaskEvent.entity_id == entity_id,
            TaskEvent.origin == "user",
            TaskEvent.status == "applied",
        )
        .order_by(TaskEvent.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def recent_user_events(db: Session, *, task_id: str, limit: int = 5) -> list[TaskEvent]:
    """This task's most recent human corrections — origin='user',
    status='applied' events, newest first (spec §4.6's learning loop, Task 2).

    These are exactly the events `edit_entity_state` (api/tasks.py's manual
    state-edit endpoint) creates — the same events `latest_applied_user_event`
    already uses as the extraction validator's correction fence. This query
    feeds the OTHER half of that same signal into the extraction prompt
    itself (task_engine.engine.extract_for_pair): rather than only fencing a
    stale proposal out after the fact, recent corrections are surfaced to the
    LLM up front so it's less likely to propose relitigating one at all.
    """
    stmt = (
        select(TaskEvent)
        .where(
            TaskEvent.task_id == task_id,
            TaskEvent.origin == "user",
            TaskEvent.status == "applied",
        )
        .order_by(TaskEvent.created_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def find_event_for_message_field(
    db: Session, *, task_id: str, message_id: str | None, field: str | None
) -> TaskEvent | None:
    """SELECT-first idempotency check for (task_id, message_id, field) — the
    extraction validator's fast path (spec §4.4 step 7). The partial unique
    index `uq_task_event_msg_field` (message_id IS NOT NULL) is only the race
    backstop for migrated DBs; it does not exist in the `create_all` test
    fixture. message_id=None (user edits) never collides — this always
    returns None for it, matching the index's own `WHERE message_id IS NOT
    NULL` exemption."""
    if message_id is None:
        return None
    stmt = select(TaskEvent).where(
        TaskEvent.task_id == task_id,
        TaskEvent.message_id == message_id,
        TaskEvent.field == field,
    )
    return db.execute(stmt).scalar_one_or_none()
