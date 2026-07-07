"""Task-engine HTTP API: the goal->draft flow, tracker CRUD, the board/events
feed, and the human-correction endpoints (attach/detach, approve/reject/
revert, manual state edit, merge).

Mirrors app/api/buckets.py's shape (ownership 404s via repo.get_owned_task —
no 403-vs-404 split, so a wrong-user id and a nonexistent id look identical
on the wire; pydantic request bodies; small `_serialize_*` helpers; the
caller commits, not the repo) and app/api/inbox.py's draft-poll pattern
(mark_pending BEFORE enqueue so a fast-polling client never races a 404).

Every mutating route commits, THEN calls `_publish_task_updated` with values
read fresh off the just-committed row (`task.version` may have been bumped
in-memory by repo.apply_event/refold_entity/bump_version; pending_count is
always re-queried) — this is the one SSE event (`task_updated`) every
correction/CRUD path funnels through, matching workers/task_engine_tasks.py's
own `_publish_task_updated` helper of the same shape.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.inbox import _serialize_thread
from app.db.models import User
from app.db.session import get_db
from app.deps import get_current_user
from app.inbox import inbox_repo
from app.task_engine import criteria as criteria_mod
from app.task_engine import draft_cache
from app.task_engine import repo as task_repo
from app.task_engine import schema as schema_mod
from app.workers import task_engine_tasks
from app.workers import tasks


router = APIRouter(prefix="/api", tags=["tasks"])
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


_NO_STAGE_KEY = "(no stage)"


def _stage_counts(entities: list, *, state_schema: dict | None) -> dict:
    """Histogram of entities.state["stage"] over an already-fetched entity
    list (zero extra queries) -- key order is schema stage order (when a
    valid schema is present) with observed extras appended, then a trailing
    "(no stage)" bucket for None-staged entities. A schema-less task (or one
    whose state_schema fails validate_schema -- e.g. hand-edited or written
    by a buggy migration, see workers/task_engine_tasks.process_task_updates's
    own per-task isolation for this same failure mode) falls back to observed
    order only. Stages with zero entities are omitted entirely, so an empty
    task returns {}."""
    schema_stages: list[str] = []
    if state_schema is not None:
        try:
            schema_stages = schema_mod.validate_schema(state_schema).all_stages()
        except ValueError:
            schema_stages = []

    counts: dict[str, int] = {}
    observed_order: list[str] = []
    no_stage_count = 0
    for entity in entities:
        stage = entity.state.get("stage")
        if stage is None:
            no_stage_count += 1
            continue
        if stage not in counts:
            observed_order.append(stage)
        counts[stage] = counts.get(stage, 0) + 1

    stage_counts: dict[str, int] = {}
    for stage in schema_stages:
        if stage in counts:
            stage_counts[stage] = counts[stage]
    for stage in observed_order:
        if stage not in stage_counts:
            stage_counts[stage] = counts[stage]
    if no_stage_count:
        stage_counts[_NO_STAGE_KEY] = no_stage_count
    return stage_counts


def _serialize_summary(db: Session, *, task) -> dict:
    entities = task_repo.list_entities(db, task_id=task.id)
    latest = task_repo.list_events(db, task_id=task.id, limit=1)
    return {
        "entities": len(entities),
        "pending_reviews": task_repo.pending_count(db, task_id=task.id),
        "last_event_at": latest[0].created_at if latest else None,
        "stage_counts": _stage_counts(entities, state_schema=task.state_schema),
    }


def _serialize_task(task) -> dict:
    return {
        "id": task.id,
        "name": task.name,
        "goal": task.goal,
        "kind": task.kind,
        "status": task.status,
        "version": task.version,
    }


def _serialize_task_list_item(db: Session, task) -> dict:
    return {**_serialize_task(task), "summary": _serialize_summary(db, task=task)}


def _serialize_task_detail(db: Session, task) -> dict:
    return {
        **_serialize_task(task),
        "state_schema": task.state_schema,
        # criteria is intentionally detail-only, not on _serialize_task (the
        # list-item base) — the spec §4.6 learning loop grows this text with
        # every attach/detach example (up to EXAMPLE_CAP=30 blocks), and
        # surfacing it here is what makes that growth auditable from a
        # single task's page (minor #4, final-review wave) without bloating
        # every row of the task list with criteria text nobody reads there.
        "criteria": task.criteria,
        "summary": _serialize_summary(db, task=task),
    }


def _serialize_entity(entity) -> dict:
    return {
        "id": entity.id,
        "entity_key": entity.entity_key,
        "display_name": entity.display_name,
        "state": entity.state,
        "updated_at": entity.updated_at,
    }


def _serialize_event(event) -> dict:
    """All provenance fields — the audit trail the review queue and
    per-entity history view render straight off of. pending_reason/
    proposed_entity are the pending-provenance fields the review tray reads
    to explain WHY a pending_review event is sitting there (near-duplicate,
    backward move, terminal lock, correction fence, or plain low confidence)
    — both None for applied/rejected/reverted events."""
    return {
        "id": event.id,
        "field": event.field,
        "old_value": event.old_value,
        "new_value": event.new_value,
        "evidence_quote": event.evidence_quote,
        "confidence": event.confidence,
        "origin": event.origin,
        "status": event.status,
        "thread_id": event.thread_id,
        "message_id": event.message_id,
        "gmail_message_id": event.gmail_message_id,
        "entity_id": event.entity_id,
        "pending_reason": event.pending_reason,
        "proposed_entity": event.proposed_entity,
        "created_at": event.created_at,
    }


def _publish_task_updated(db: Session, *, user_id: str, task) -> None:
    """The one `task_updated` SSE publish every mutating route ends with,
    read fresh off the just-committed row (see module docstring)."""
    tasks._publish(user_id, "task_updated", {
        "task_id": task.id,
        "version": task.version,
        "pending_count": task_repo.pending_count(db, task_id=task.id),
    })


def _require_owned_task(db: Session, *, user_id: str, task_id: str):
    task = task_repo.get_owned_task(db, user_id=user_id, task_id=task_id)
    if task is None:
        raise HTTPException(404, "not found")
    return task


# ---------------------------------------------------------------------------
# Draft: goal -> proposed schema/criteria (mirrors buckets' draft/preview)
# ---------------------------------------------------------------------------


class _DraftBody(BaseModel):
    goal: str = Field(min_length=1)


@router.post("/tasks/draft", status_code=202)
def post_task_draft(body: _DraftBody, user: User = Depends(get_current_user)) -> dict:
    """Enqueue a goal -> proposed task draft and return a draft_id.

    Same two delivery paths as bucket draft/preview: an SSE `task_draft_ready`
    push for the fast case, and this draft_id as the polling fallback key.
    """
    draft_id = uuid.uuid4().hex
    # mark pending BEFORE enqueueing so a fast-polling client never races the
    # worker's first redis write and sees a 404.
    draft_cache.mark_pending(draft_id, user_id=user.id)
    task_engine_tasks.propose_task_draft.apply_async(
        args=[user.id, draft_id, body.goal], countdown=0,
    )
    return {"draft_id": draft_id}


@router.get("/tasks/draft/{draft_id}")
def get_task_draft(draft_id: str, response: Response,
                   user: User = Depends(get_current_user)) -> dict:
    """200 ready payload | 202 {"status":"pending"} | 404 | 403 — mirrors
    GET /api/buckets/draft/preview/{draft_id} exactly."""
    entry = draft_cache.load(draft_id)
    if entry is None:
        raise HTTPException(404, "not found")
    if entry.get("user_id") != user.id:
        raise HTTPException(403, "not your draft")
    if entry.get("status") == "pending":
        response.status_code = 202
        return {"status": "pending"}
    return {
        "status": "ready",
        "proposal": entry.get("proposal"),
        "positives": entry.get("positives", []),
        "near_misses": entry.get("near_misses", []),
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class _ExampleIn(BaseModel):
    sender: str = ""
    subject: str = ""
    snippet: str = ""
    rationale: str = ""


class _CreateTaskBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    goal: str = Field(min_length=1)
    description: str = Field(min_length=1)
    state_schema: dict
    keyword_probes: list[str] = Field(default_factory=list)
    confirmed_positives: list[_ExampleIn] = Field(default_factory=list)
    confirmed_negatives: list[_ExampleIn] = Field(default_factory=list)


class _PatchTaskBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = None
    state_schema: dict | None = None


@router.get("/tasks")
def list_tasks(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    rows = task_repo.list_tasks(db, user_id=user.id)
    return {"tasks": [_serialize_task_list_item(db, t) for t in rows]}


@router.post("/tasks", status_code=201)
def create_task(body: _CreateTaskBody, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)) -> dict:
    try:
        schema = schema_mod.validate_schema(body.state_schema)
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    criteria = criteria_mod.formulate_criteria(
        description=body.description,
        confirmed_positives=[e.model_dump() for e in body.confirmed_positives],
        confirmed_negatives=[e.model_dump() for e in body.confirmed_negatives],
    )
    task = task_repo.create_task(
        db, user_id=user.id, name=body.name, goal=body.goal, criteria=criteria,
        state_schema=schema.model_dump(), kind="tracker",
    )
    db.commit()
    # Async — the user gets 201 immediately; a newly created tracker is run
    # over the user's stored history in the background (progress arrives via
    # task_backfill_progress SSE events, see workers/task_engine_tasks.py).
    task_engine_tasks.backfill_task.apply_async(
        args=[user.id, task.id, body.keyword_probes], countdown=0,
    )
    _publish_task_updated(db, user_id=user.id, task=task)
    return _serialize_task_detail(db, task)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, user: User = Depends(get_current_user),
            db: Session = Depends(get_db)) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    return _serialize_task_detail(db, task)


@router.patch("/tasks/{task_id}")
def patch_task(task_id: str, body: _PatchTaskBody, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)

    if body.name is not None:
        task.name = body.name

    if body.status is not None:
        if body.status not in ("active", "paused"):
            raise HTTPException(422, "status must be 'active' or 'paused'")
        task.status = body.status

    if body.state_schema is not None:
        try:
            new_schema = schema_mod.validate_schema(body.state_schema)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if task.state_schema is not None:
            old_schema = schema_mod.validate_schema(task.state_schema)
            try:
                schema_mod.assert_additive_change(old_schema, new_schema)
            except ValueError as exc:
                raise HTTPException(409, str(exc))
        task.state_schema = new_schema.model_dump()

    # Bump version on every successful PATCH — not just state_schema changes.
    # `version` means "task state a client may need to refetch"; the 2B
    # client's version-gap refetch compares the version on each task_updated
    # SSE event to the last one it saw and only refetches when it's NEWER, so
    # a name- or status-only PATCH that left version unchanged would publish
    # task_updated with a stale version and the client would skip the
    # refetch, keeping stale name/status. We bump once per successful PATCH
    # regardless of which field(s) changed (even a no-op body that matches
    # the current fields still bumps) — simpler than diffing old vs new
    # values field-by-field, and an extra refetch is harmless.
    task_repo.bump_version(db, task=task)

    db.commit()
    _publish_task_updated(db, user_id=user.id, task=task)
    return _serialize_task_detail(db, task)


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: str, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)) -> None:
    """Soft delete, idempotent like buckets' DELETE: a second call against
    the same (owned) task_id is a silent no-op, not a 404 — get_owned_task's
    is_deleted filter can't tell that case apart from "not yours", so we fall
    back to get_owned_task_any_status to distinguish them without leaking
    existence across users (a wrong-user id still 404s)."""
    task = task_repo.get_owned_task(db, user_id=user.id, task_id=task_id)
    if task is not None:
        task_repo.soft_delete_task(db, task=task)
        # Bump version before the commit below so the publish carries the
        # new version — a cross-session TaskDetail open on this task
        # refetches on the version gap, and that refetch's GET
        # /api/tasks/{id} 404s (get_owned_task excludes soft-deleted rows),
        # which the provider treats as eviction. Without this bump, the
        # publish would carry the task's pre-delete version, the other
        # session's version-gap check would see no gap, and that TaskDetail
        # would show a deleted task forever.
        task_repo.bump_version(db, task=task)
        db.commit()
        _publish_task_updated(db, user_id=user.id, task=task)
        return

    existing = task_repo.get_owned_task_any_status(db, user_id=user.id, task_id=task_id)
    if existing is None:
        raise HTTPException(404, "not found")
    # Already deleted by this same user — idempotent no-op, nothing changed
    # to publish.
    return


# ---------------------------------------------------------------------------
# Board + events feed
# ---------------------------------------------------------------------------


@router.get("/tasks/{task_id}/board")
def get_task_board(task_id: str, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    entities = task_repo.list_entities(db, task_id=task.id)
    return {"entities": [_serialize_entity(e) for e in entities]}


@router.get("/tasks/{task_id}/events")
def list_task_events(
    task_id: str,
    status: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    offset = (page - 1) * limit
    events = task_repo.list_events(
        db, task_id=task.id, status=status, entity_id=entity_id,
        limit=limit, offset=offset,
    )
    return {"events": [_serialize_event(e) for e in events]}


# ---------------------------------------------------------------------------
# Threads: list / user-attach / user-detach
# ---------------------------------------------------------------------------


class _AttachThreadBody(BaseModel):
    thread_id: str = Field(min_length=1)
    add_example: bool = True


def _example_from_thread(db: Session, *, user_id: str, thread, rationale: str) -> dict | None:
    """Build an `append_example()` input dict from a thread's most-recent
    message (spec §4.6 learning loop, Task 2) — same recent-message
    resolution `_serialize_thread`/`task_engine_tasks._candidate_from_thread`
    already use elsewhere: `thread.recent_message_id` is a soft pointer, so
    it's resolved via `inbox_repo.get_message` scoped to `user_id`. Returns
    None (caller skips the append silently) when the thread has no usable
    recent message yet to quote."""
    if not thread.recent_message_id:
        return None
    msg = inbox_repo.get_message(db, user_id=user_id, message_id=thread.recent_message_id)
    if msg is None:
        return None
    return {
        "sender": msg.from_addr or "",
        "subject": thread.subject or "",
        "snippet": msg.body_preview or "",
        "rationale": rationale,
    }


@router.get("/tasks/{task_id}/threads")
def list_task_threads(task_id: str, user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    thread_ids = task_repo.list_attached_thread_ids(db, task_id=task.id)
    threads = inbox_repo.get_threads_batch(db, user_id=user.id, thread_ids=list(thread_ids))
    return {"threads": [_serialize_thread(db, user.id, t) for t in threads]}


@router.post("/tasks/{task_id}/threads", status_code=201)
def attach_thread(task_id: str, body: _AttachThreadBody, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    thread = inbox_repo.get_thread(db, user_id=user.id, thread_id=body.thread_id)
    if thread is None:
        raise HTTPException(404, "not found")

    task_repo.upsert_link(
        db, task_id=task.id, thread_id=thread.id, user_id=user.id,
        origin="user", state="attached",
    )
    if body.add_example:
        # spec §4.6 learning loop (Task 2): an explicit attach is a positive
        # signal the task's own classify criteria should learn from, same as
        # a confirmed-positive example at task-creation time.
        example = _example_from_thread(
            db, user_id=user.id, thread=thread,
            rationale="user attached this thread to the task",
        )
        if example is not None:
            task.criteria = criteria_mod.append_example(task.criteria, example=example, tag="positive")
    # Bump version unconditionally: attaching a thread changes what this
    # task tracks even though it touches no entity/event state, and this
    # route otherwise never bumps (see PATCH's own bump_version comment for
    # the rationale — every other mutating route must too, or a cross-
    # session TaskDetail's version-gap SSE refetch never fires and its
    # threads panel silently misses the new attachment).
    task_repo.bump_version(db, task=task)
    db.commit()
    # Extract this one pair immediately rather than waiting for the next
    # sync-triggered process_task_updates run (see workers/task_engine_tasks
    # .extract_for_thread's docstring — this is exactly the entrypoint it
    # was added for).
    task_engine_tasks.extract_for_thread.apply_async(
        args=[user.id, task.id, thread.id], countdown=0,
    )
    _publish_task_updated(db, user_id=user.id, task=task)
    return _serialize_thread(db, user.id, thread)


@router.delete("/tasks/{task_id}/threads/{thread_id}", status_code=204)
def detach_thread(task_id: str, thread_id: str, add_example: bool = Query(default=True),
                  user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)) -> None:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    thread = inbox_repo.get_thread(db, user_id=user.id, thread_id=thread_id)
    if thread is None:
        raise HTTPException(404, "not found")

    task_repo.upsert_link(
        db, task_id=task.id, thread_id=thread.id, user_id=user.id,
        origin="user", state="detached",
    )
    if add_example:
        # spec §4.6 learning loop (Task 2): an explicit detach is a
        # near-miss signal — the thread looked relevant enough to have been
        # linked, but the user says it isn't.
        example = _example_from_thread(
            db, user_id=user.id, thread=thread,
            rationale="user detached this thread from the task",
        )
        if example is not None:
            task.criteria = criteria_mod.append_example(task.criteria, example=example, tag="nearmiss")
    # A detached thread's not-yet-reviewed proposals must not remain
    # approvable later (the thread they cite is gone from this task) — flip
    # them to 'rejected' before the refold logic below. These were never
    # folded into any entity.state (pending_review events never are), so
    # there's nothing to refold for them specifically.
    for event in task_repo.list_pending_events_for_thread(
        db, task_id=task.id, thread_id=thread.id,
    ):
        event.status = "rejected"

    # Revert every event this task applied off of this thread, then refold
    # each entity those events touched so the board reflects the reversal
    # in the same request (no waiting for the next extraction run).
    reverted_events = task_repo.list_applied_events_for_thread(
        db, task_id=task.id, thread_id=thread.id,
    )
    touched_entity_ids: set[str] = set()
    for event in reverted_events:
        event.status = "reverted"
        if event.entity_id:
            touched_entity_ids.add(event.entity_id)
    # Same autoflush=False caveat as revert_event above — refold_entity's
    # SELECT must see every status flip made in the loop just above.
    db.flush()
    for entity_id in touched_entity_ids:
        entity = task_repo.get_entity(db, task_id=task.id, entity_id=entity_id)
        if entity is not None:
            task_repo.refold_entity(db, task=task, entity=entity)

    # Bump version unconditionally. refold_entity above already bumps when it
    # actually reverted >=1 applied event, but a detach with nothing to
    # revert (no applied events on this thread) would otherwise leave
    # version untouched even though the thread's attachment state changed
    # and its still-pending events were just auto-rejected above — both
    # invisible to a cross-session TaskDetail's version-gap SSE refetch
    # without this. A second bump on top of refold's is harmless (one extra
    # refetch, not incorrect data) — simpler than branching on whether
    # refold actually ran.
    task_repo.bump_version(db, task=task)
    db.commit()
    _publish_task_updated(db, user_id=user.id, task=task)


# ---------------------------------------------------------------------------
# Event corrections: approve / reject / revert
# ---------------------------------------------------------------------------


def _require_owned_event(db: Session, *, task, event_id: str):
    event = task_repo.get_event(db, task_id=task.id, event_id=event_id)
    if event is None:
        raise HTTPException(404, "not found")
    return event


def _require_event_entity(db: Session, *, task, event):
    # Every event that reaches applied/pending_review has a real entity_id
    # (see transitions._process_one step 8) — this is a defensive lookup,
    # not an expected-empty branch.
    entity = task_repo.get_entity(db, task_id=task.id, entity_id=event.entity_id) if event.entity_id else None
    if entity is None:
        raise HTTPException(404, "entity not found")
    return entity


@router.post("/tasks/{task_id}/events/{event_id}/approve")
def approve_event(task_id: str, event_id: str, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    event = _require_owned_event(db, task=task, event_id=event_id)
    if event.status != "pending_review":
        raise HTTPException(409, f"event is '{event.status}', not pending_review")
    entity = _require_event_entity(db, task=task, event=event)

    # repo.refold_entity's fold key is created_at ("assertion time") — an
    # approved event may be much OLDER than events applied since it was
    # staged pending. Without re-dating it here, a LATER refold (revert/
    # detach/merge touching this same entity) would silently re-sort this
    # event back to its original position and let a newer-but-since-
    # superseded applied event's value win the fold again, contradicting the
    # user's explicit approval just made. Re-dating to now() means "the user
    # approved this NOW" always outranks anything earlier on every future
    # fold. Gmail provenance (message_id/gmail_message_id/evidence_quote) is
    # untouched — only the fold-ordering key moves.
    event.created_at = datetime.now(timezone.utc)
    task_repo.apply_event(db, task=task, entity=entity, event=event)
    db.commit()
    _publish_task_updated(db, user_id=user.id, task=task)
    return _serialize_event(event)


@router.post("/tasks/{task_id}/events/{event_id}/reject")
def reject_event(task_id: str, event_id: str, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    event = _require_owned_event(db, task=task, event_id=event_id)
    if event.status != "pending_review":
        raise HTTPException(409, f"event is '{event.status}', not pending_review")

    event.status = "rejected"
    # The validator mints an entity row even for a pending_review outcome
    # (step 8 needs a real entity_id for either branch) — if this reject was
    # the entity's ONLY event and nothing was ever folded into its state,
    # rejecting it just stranded an empty, permanently-visible board row
    # with no way to remove it later. Clean that up here; an entity with
    # other history (an applied event, another still-pending proposal, a
    # manual state edit) is left untouched. This doesn't bump task.version
    # or need its own SSE nudge beyond the pending_count change already
    # carried by _publish_task_updated below — the client's TaskDetail
    # already refetches the board on reject, so a vanished entity is picked
    # up on that refetch.
    if event.entity_id is not None:
        task_repo.delete_entity_if_orphaned(
            db, task_id=task.id, entity_id=event.entity_id, excluding_event_id=event.id,
        )
    db.commit()
    _publish_task_updated(db, user_id=user.id, task=task)
    return _serialize_event(event)


@router.post("/tasks/{task_id}/events/{event_id}/revert")
def revert_event(task_id: str, event_id: str, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    event = _require_owned_event(db, task=task, event_id=event_id)
    if event.status != "applied":
        raise HTTPException(409, f"event is '{event.status}', not applied")
    entity = _require_event_entity(db, task=task, event=event)

    event.status = "reverted"
    # refold_entity re-SELECTs this task's applied events; sessions here run
    # with autoflush=False (see task_engine/repo.py's module docstring), so
    # the status flip above must be flushed before that query runs or it
    # would still see this event as 'applied'.
    db.flush()
    task_repo.refold_entity(db, task=task, entity=entity)
    db.commit()
    _publish_task_updated(db, user_id=user.id, task=task)
    return _serialize_event(event)


# ---------------------------------------------------------------------------
# Entities: manual state edit + merge
# ---------------------------------------------------------------------------


class _StateEditBody(BaseModel):
    field: str = Field(min_length=1)
    value: str


@router.post("/tasks/{task_id}/entities/{entity_id}/state")
def edit_entity_state(task_id: str, entity_id: str, body: _StateEditBody,
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    entity = task_repo.get_entity(db, task_id=task.id, entity_id=entity_id)
    if entity is None:
        raise HTTPException(404, "not found")
    if task.state_schema is None:
        raise HTTPException(422, "task has no state_schema yet")

    schema = schema_mod.validate_schema(task.state_schema)
    if body.field == "stage":
        if body.value not in schema.all_stages():
            raise HTTPException(422, f"'{body.value}' is not a valid stage")
        new_value = body.value
    else:
        attr = schema.attr(body.field)
        if attr is None:
            raise HTTPException(422, f"unknown field '{body.field}'")
        try:
            new_value = schema_mod.coerce_value(attr.type, body.value, enum_values=attr.values)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    old_value = entity.state.get(body.field)
    # A user-origin applied event, with no message/thread provenance (this
    # wasn't extracted from a thread) — and, per transitions.py's fence rule
    # (latest_applied_user_event), THIS event becomes the fence: any LLM
    # proposal whose evidence message isn't strictly newer than this event's
    # created_at is forced to pending_review from here on.
    event = task_repo.append_event(
        db, task=task, entity=entity, origin="user", status="applied",
        field=body.field, old_value=old_value, new_value=new_value,
    )
    task_repo.apply_event(db, task=task, entity=entity, event=event)
    db.commit()
    _publish_task_updated(db, user_id=user.id, task=task)
    return _serialize_entity(entity)


class _MergeBody(BaseModel):
    into_entity_id: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Aggregate feeds: reviews + activity (Task 3, Phase 3 HUD inversion)
#
# Cross-task reads for the HUD's unified review tray and activity ticker.
# Both are user-scoped joins on Task (repo.list_pending_events_for_user /
# list_recent_events_for_user) — a cross-user leak here would expose other
# people's email-derived text, so this is a security boundary, not just a
# convenience query; treat it with the same care as any other ownership
# check in this file.
# ---------------------------------------------------------------------------


_REVIEWS_LIMIT_MIN, _REVIEWS_LIMIT_MAX, _REVIEWS_LIMIT_DEFAULT = 1, 200, 50
_ACTIVITY_LIMIT_MIN, _ACTIVITY_LIMIT_MAX, _ACTIVITY_LIMIT_DEFAULT = 1, 100, 20


def _entity_display_names_for(db: Session, pairs: list[tuple]) -> dict[str, str]:
    entity_ids = {event.entity_id for event, _task in pairs if event.entity_id is not None}
    return task_repo.get_entity_display_names(db, entity_ids=entity_ids)


def _serialize_feed_event(event, *, task, display_names: dict[str, str]) -> dict:
    """_serialize_event's fields plus the cross-task fields the aggregate
    feeds need: task_id/task_name so the HUD can route back to the owning
    task, and entity_display_name resolved via a fallback chain — the
    batch-resolved TaskStateEntity.display_name (absent if the entity's
    since been hard-deleted, e.g. reject's orphan cleanup on a since-
    rejected event), else the LLM's verbatim proposed_entity string
    recorded at proposal time, else null."""
    entity_display_name = (
        display_names.get(event.entity_id) if event.entity_id is not None else None
    )
    if entity_display_name is None:
        entity_display_name = event.proposed_entity
    return {
        **_serialize_event(event),
        "task_id": task.id,
        "task_name": task.name,
        "entity_display_name": entity_display_name,
    }


@router.get("/reviews")
def get_reviews(limit: int = Query(default=_REVIEWS_LIMIT_DEFAULT),
                user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """Unified review tray: every still-pending_review event across all of
    this user's non-deleted tasks, newest first, regardless of task.
    limit is clamped (never 422'd) to [1, 200]."""
    limit = max(_REVIEWS_LIMIT_MIN, min(_REVIEWS_LIMIT_MAX, limit))
    pairs = task_repo.list_pending_events_for_user(db, user_id=user.id, limit=limit)
    display_names = _entity_display_names_for(db, pairs)
    return {
        "reviews": [_serialize_feed_event(event, task=task, display_names=display_names)
                   for event, task in pairs],
    }


@router.get("/activity")
def get_activity(limit: int = Query(default=_ACTIVITY_LIMIT_DEFAULT),
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """Activity ticker: every non-pending_review event (applied/rejected/
    reverted) across this user's non-deleted tasks, newest first. limit is
    clamped (never 422'd) to [1, 100]."""
    limit = max(_ACTIVITY_LIMIT_MIN, min(_ACTIVITY_LIMIT_MAX, limit))
    pairs = task_repo.list_recent_events_for_user(db, user_id=user.id, limit=limit)
    display_names = _entity_display_names_for(db, pairs)
    return {
        "activity": [_serialize_feed_event(event, task=task, display_names=display_names)
                     for event, task in pairs],
    }


@router.post("/tasks/{task_id}/entities/{entity_id}/merge", status_code=204)
def merge_entity(task_id: str, entity_id: str, body: _MergeBody,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    if entity_id == body.into_entity_id:
        raise HTTPException(422, "cannot merge an entity into itself")

    loser = task_repo.get_entity(db, task_id=task.id, entity_id=entity_id)
    winner = task_repo.get_entity(db, task_id=task.id, entity_id=body.into_entity_id)
    if loser is None or winner is None:
        raise HTTPException(404, "not found")

    task_repo.repoint_entity_events(
        db, task_id=task.id, from_entity_id=loser.id, to_entity_id=winner.id,
    )
    task_repo.refold_entity(db, task=task, entity=winner)
    task_repo.delete_entity(db, entity=loser)
    db.commit()
    _publish_task_updated(db, user_id=user.id, task=task)
