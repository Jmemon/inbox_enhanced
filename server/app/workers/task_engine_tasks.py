"""Decoupled Celery module for task-engine extraction.

Kept separate from `workers/tasks.py` (Gmail sync) on purpose: extraction is
LLM-latency-bound and per-tracker fan-out, whereas sync is Gmail-API-bound —
mixing them onto one task module would make a slow extraction run block (or
compete with) the sync queue's own throughput.

`workers/tasks.py` imports this module at its own top level (the sync-enqueue
hook) — that means THIS module must never import `app.workers.tasks` at its
own top level, or the two modules would form an import cycle. `_publish` is
therefore pulled in with a late import inside each task function's body
instead of a top-level `from app.workers.tasks import _publish`.

No `sync_lock` anywhere in this module: neither task ever writes
`inbox_threads`/`inbox_messages`, so there's nothing here that can race the
sync path's `(user_id, gmail_id)` unique constraint. Idempotency instead comes
entirely from `transitions.validate_and_stage`'s step 7 (SELECT-first check
against `(task_id, message_id, field)`, backed by the migrated DB's partial
unique index as a race backstop) — re-running either task against the same
(task, thread) pair is always safe and produces no duplicate events.
"""

import logging

from app.db.session import SessionLocal as _AppSessionLocal
from app.task_engine import repo as task_repo
from app.task_engine.engine import extract_for_pair
from app.workers.celery_app import celery_app

# Module-level seam so tests can rebind onto an in-memory engine, matching
# workers/tasks.py's convention.
SessionLocal = _AppSessionLocal
log = logging.getLogger(__name__)


def _publish_task_updated(db, *, user_id: str, task) -> None:
    """One `task_updated` publish for this task, via workers.tasks._publish.
    Late-imported (see module docstring) to break the tasks<->task_engine_tasks
    cycle."""
    from app.workers.tasks import _publish

    _publish(user_id, "task_updated", {
        "task_id": task.id,
        "version": task.version,
        "pending_count": task_repo.pending_count(db, task_id=task.id),
    })


@celery_app.task(name="app.workers.task_engine_tasks.process_task_updates")
def process_task_updates(user_id: str, thread_ids: list[str]) -> None:
    """Run extraction for every (active tracker, touched thread) pair whose
    link is currently attached.

    For each active, schema-bearing tracker (`list_active_trackers` already
    excludes paused/bucket-kind/schema-less tasks), intersect its currently
    `attached` thread links against the sync-touched `thread_ids` — a
    `state='detached'` link is silently excluded by that intersection, not by
    a special case here. Each surviving pair is extracted sequentially
    (`extract_for_pair` -> `db.commit()`), and exactly ONE `task_updated`
    publish is emitted per task at the end of its pairs — never one per pair,
    which would spam a client with N SSE events for a single sync tick.

    A task whose run produced zero pending_review events AND no version
    change (i.e. nothing applied either, since every applied event bumps
    `task.version`) is skipped entirely — a reclassify/poll that touched none
    of this task's relevant threads (or whose extraction found nothing new)
    must not wake a client with a no-op event. This is also what makes a
    re-run over the same input idempotent from the client's point of view:
    the validator's own idempotency check (step 7) means a repeat run stages
    nothing new, `any_pending` stays False, and the version is unchanged, so
    no second publish fires.

    One bad tracker must not poison the whole batch: the entire per-task body
    below is wrapped in its own try/except. `extract_for_pair` calls
    `schema.validate_schema(task.state_schema)` uncaught, so a tracker whose
    `state_schema` is corrupted (e.g. hand-edited or written by a buggy
    migration) raises there — without isolation, that exception would
    propagate out of this `for task in ...` loop entirely and starve every
    sibling tracker for this user of its extraction run. On catch we
    `db.rollback()` (a failed flush mid-pair must not poison the session for
    the next task — per-pair commits mean only the failed pair's uncommitted
    work rolls back) and move on to the next task.
    """
    touched = set(thread_ids)
    if not touched:
        return
    db = SessionLocal()
    try:
        for task in task_repo.list_active_trackers(db, user_id=user_id):
            try:
                attached = task_repo.list_attached_thread_ids(db, task_id=task.id)
                pairs = sorted(attached & touched)
                if not pairs:
                    continue

                version_before = task.version
                any_pending = False
                for thread_id in pairs:
                    staged = extract_for_pair(
                        db, task=task, thread_internal_id=thread_id, user_id=user_id,
                    )
                    if staged is None:
                        continue
                    if staged.pending:
                        any_pending = True
                    db.commit()

                if not any_pending and task.version == version_before:
                    log.info(
                        "process_task_updates: task=%s pairs=%d no change, skipping publish",
                        task.id, len(pairs),
                    )
                    continue

                _publish_task_updated(db, user_id=user_id, task=task)
            except Exception:
                log.exception(
                    "process_task_updates: task %s failed; continuing", task.id,
                )
                db.rollback()
                continue
    finally:
        db.close()


@celery_app.task(name="app.workers.task_engine_tasks.extract_for_thread")
def extract_for_thread(user_id: str, task_id: str, thread_id: str) -> None:
    """Single-pair extraction variant — used by the user-initiated attach
    flow (Task 10) so a thread the user manually links to a tracker is
    extracted immediately, without waiting for the next sync-triggered
    `process_task_updates` run. Same commit-then-publish shape as that task,
    scoped to the one task it's given."""
    db = SessionLocal()
    try:
        task = task_repo.get_owned_task(db, user_id=user_id, task_id=task_id)
        # kind != "tracker" guard matches list_active_trackers' implicit
        # filter on the batch path (Task 7 review fix) — this single-pair
        # entrypoint has no such filter of its own otherwise, so a bucket-
        # kind task passed in here would silently run tracker extraction
        # against it.
        if (
            task is None
            or task.kind != "tracker"
            or task.status != "active"
            or task.state_schema is None
        ):
            log.info(
                "extract_for_thread: task=%s not an active schema-bearing tracker, skipping",
                task_id,
            )
            return

        version_before = task.version
        staged = extract_for_pair(db, task=task, thread_internal_id=thread_id, user_id=user_id)
        if staged is None:
            return
        db.commit()

        if not staged.pending and task.version == version_before:
            log.info("extract_for_thread: task=%s no change, skipping publish", task.id)
            return
        _publish_task_updated(db, user_id=user_id, task=task)
    finally:
        db.close()
