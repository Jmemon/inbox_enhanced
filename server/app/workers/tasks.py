"""Celery task definitions.

Each task opens its own SQLAlchemy session (workers run outside the FastAPI
request lifecycle, so they can't lean on Depends(get_db)).

`SessionLocal` is referenced as a module-level attribute so tests can monkey-
patch it onto an in-memory engine.

poll_new_messages owns the history.list call so it can:
 - return silently when there are no new records (don't publish on noise),
 - fall back to full_sync when gmail 404s (cursor expired past the ~30 day
   retention window), and
 - hand the records through to partial_sync_inbox to avoid a redundant fetch.
"""

import asyncio
import json
import logging
import zlib
from sqlalchemy import select
from app.config import get_settings
from app.db.session import SessionLocal as _AppSessionLocal
from app.db.models import User, InboxThread, InboxMessage, Task
from app.realtime import active_users, last_sync, sync_lock
from app.realtime import redis_client as _redis_client
from app.gmail.client import get_gmail_client
from app.gmail.parser import thread_to_string
from app.inbox import inbox_repo
from app.llm import client as llm_client
from app.llm.prompts import score_thread
from app.workers import gmail_sync, task_engine_tasks
from app.workers.celery_app import celery_app


SessionLocal = _AppSessionLocal
log = logging.getLogger(__name__)

# --- bucket-draft-preview scoring constants ---
# draft_preview_bucket itself was deleted in Phase 4 Task 3 (superseded by
# the task engine's goal->draft flow), but _read_candidates/_score_all below
# are still late-imported by workers/task_engine_tasks.py's propose_task_draft
# (the SAME 0-10 scoring rubric, reused for a tracker's proposed name/
# description instead of a bucket's) -- these thresholds stay put.
# Score thresholds that define positive vs. near-miss.
POSITIVE_THRESHOLD = 7
NEAR_MISS_LOW = 4
NEAR_MISS_HIGH = 6


def _publish(user_id: str, event: str, payload: dict) -> None:
    """Typed publish — finalised in Task 15. All publishers go through here.

    Logs the redis subscriber count returned by `publish` so operations can
    diagnose delivery failures: subscribers=0 means no SSE-side dispatcher
    was listening on this user's channel at publish time (e.g. due to
    subscribe/unsubscribe churn during SSE flapping). The published frame
    is silently dropped by redis when nobody is subscribed.
    """
    body = json.dumps({"event": event, **payload})
    n = _redis_client.get_redis().publish(f"user:{user_id}", body)
    log.info("publish: user=%s event=%s subscribers=%d bytes=%d",
             user_id, event, n, len(body))


def _publish_thread_ids(user_id: str, thread_ids: list[str]) -> None:
    if not thread_ids:
        return
    log.info("_publish_thread_ids: user=%s count=%d", user_id, len(thread_ids))
    _publish(user_id, "threads_updated", {"thread_ids": thread_ids})


@celery_app.task(name="app.workers.tasks.enqueue_polls")
def enqueue_polls() -> None:
    """Beat fan-out: purge expired entries, then enqueue one poll per active user."""
    active_users.purge_expired()
    active = list(active_users.list_active())
    log.info("enqueue_polls: found %d active users: %s", len(active), active)
    for uid in active:
        # Random 0-10s spread happens at apply_async time. Use a fixed countdown of
        # 0 here for determinism in tests; production beat schedule could randomize.
        poll_new_messages.apply_async(args=[uid], countdown=0)


@celery_app.task(name="app.workers.tasks.enqueue_tracker_owner_polls")
def enqueue_tracker_owner_polls() -> None:
    """Hourly beat fan-out: poll tracker owners even with no open tab.

    enqueue_polls (30s) only covers active_users — users with a live SSE
    connection. A tracker keeps advancing only when its owner's inbox gets
    synced, so an owner who closes their tab would otherwise have their
    tracker go stale until they next open the app. This picks up the slack:
    every user with at least one active, non-deleted, schema-bearing tracker
    gets a poll enqueued, skipping anyone already covered by the 30s path.

    One DISTINCT query, early return when nothing qualifies — cheap on the
    single-replica beat.  Countdown is `crc32(user_id) % 3600`, a
    deterministic hash (unlike the process-salted builtin hash()) so a given
    user always lands at the same offset within the hour instead of every
    tracker owner's poll firing in the same instant.
    """
    db = SessionLocal()
    try:
        uids = db.execute(
            select(Task.user_id)
            .where(
                Task.kind == "tracker",
                Task.status == "active",
                Task.is_deleted == False,  # noqa: E712
                Task.user_id.isnot(None),
                Task.state_schema.isnot(None),
            )
            .distinct()
        ).scalars().all()
    finally:
        db.close()

    if not uids:
        return

    # Purge expired entries before reading active_users; stale entries from
    # unclean SSE disconnects would otherwise mask offline tracker owners from
    # the hourly poll path indefinitely.
    active_users.purge_expired()
    active = set(active_users.list_active())
    log.info("enqueue_tracker_owner_polls: %d tracker owner(s), %d already active",
             len(uids), len(active))
    for uid in uids:
        if uid in active:
            continue
        countdown = zlib.crc32(uid.encode()) % 3600
        poll_new_messages.apply_async(args=[uid], countdown=countdown)


@celery_app.task(name="app.workers.tasks.poll_new_messages")
def poll_new_messages(user_id: str) -> None:
    """Sync new messages for one user and publish updated thread ids.

    Flow:
     1. No history cursor yet → full_sync_inbox (bootstrap).
     2. Cursor present → call history.list:
        - 404 → HistoryGoneError → full_sync_inbox (recovery).
        - empty records → return silently.
        - records → partial_sync_inbox(history_records, new_history_id).
     3. Publish ALL touched thread ids on user:{user_id}.
     4. Enqueue process_task_updates with CONTENT ids only (the subset whose
        full content was actually fetched this round) — a flag-only touch
        (unread flip, archive/unarchive, soft-delete) has no new content for a
        tracker to extract from; enqueueing it anyway spends a Sonnet
        extraction call that dedupes against unchanged evidence only after
        the cost is paid, and 404-recovery full_sync_inbox would amplify that
        waste up to 200x. See gmail_sync module docstring.

    Holds a per-user redis lock for the duration so a concurrent
    full_sync_inbox_task or another beat-driven poll can't race on the
    (user_id, gmail_id) unique constraint and leave the inbox half-synced.
    """
    log.info("poll_new_messages: start user=%s", user_id)
    if not sync_lock.acquire(user_id):
        log.info("poll_new_messages: user=%s already syncing, skipping", user_id)
        return
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            log.warning("poll_new_messages: user %s not found", user_id)
            return

        if not user.gmail_last_history_id:
            log.info("poll_new_messages: user=%s has no history cursor → full sync", user_id)
            all_ids, content_ids = gmail_sync.full_sync_inbox(db, user=user)
            log.info("poll_new_messages: user=%s full sync complete, publishing %d ids", user_id, len(all_ids))
            _publish_thread_ids(user_id, all_ids)
            if content_ids:
                task_engine_tasks.process_task_updates.apply_async(args=[user_id, content_ids], countdown=0)
            last_sync.mark(user_id)
            return

        gmail = get_gmail_client(db, user)
        try:
            history_records, new_history_id = gmail_sync.fetch_history_records(
                gmail, start_history_id=user.gmail_last_history_id,
            )
        except gmail_sync.HistoryGoneError:
            log.info("poll_new_messages: history 404 for %s; falling back to full sync", user_id)
            all_ids, content_ids = gmail_sync.full_sync_inbox(db, user=user)
            log.info("poll_new_messages: user=%s recovery full sync complete, publishing %d ids", user_id, len(all_ids))
            _publish_thread_ids(user_id, all_ids)
            if content_ids:
                task_engine_tasks.process_task_updates.apply_async(args=[user_id, content_ids], countdown=0)
            last_sync.mark(user_id)
            return

        if not history_records:
            log.info("poll_new_messages: user=%s history returned 0 records → no publish", user_id)
            last_sync.mark(user_id)  # a successful check IS a sync, even with nothing new
            return  # silent: no new changes

        log.info("poll_new_messages: user=%s got %d history records → partial sync", user_id, len(history_records))
        all_ids, content_ids = gmail_sync.partial_sync_inbox(
            db, user=user,
            history_records=history_records,
            new_history_id=new_history_id,
        )
        log.info("poll_new_messages: user=%s partial sync complete, publishing %d ids", user_id, len(all_ids))
        _publish_thread_ids(user_id, all_ids)
        if content_ids:
            task_engine_tasks.process_task_updates.apply_async(args=[user_id, content_ids], countdown=0)
        last_sync.mark(user_id)
    finally:
        db.close()
        sync_lock.release(user_id)


@celery_app.task(name="app.workers.tasks.full_sync_inbox")
def full_sync_inbox_task(user_id: str) -> None:
    """Explicit full-sync entry point. Used by the SSE-on-connect kickoff and
    by POST /api/inbox/refresh when the user has no history cursor.

    Holds the same per-user lock poll_new_messages uses, so the SSE kickoff
    and a concurrent beat-driven poll can't both try to fan out 200 inserts
    against the unique constraint at the same time.
    """
    log.info("full_sync_inbox_task: start user=%s", user_id)
    if not sync_lock.acquire(user_id):
        log.info("full_sync_inbox_task: user=%s already syncing, skipping", user_id)
        return
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            log.warning("full_sync_inbox_task: user %s not found", user_id)
            return
        all_ids, content_ids = gmail_sync.full_sync_inbox(db, user=user)
        log.info("full_sync_inbox_task: user=%s complete, publishing %d ids", user_id, len(all_ids))
        _publish_thread_ids(user_id, all_ids)
        if content_ids:
            task_engine_tasks.process_task_updates.apply_async(args=[user_id, content_ids], countdown=0)
        last_sync.mark(user_id)
    finally:
        db.close()
        sync_lock.release(user_id)


def _read_candidates(db, *, user_id: str, exclude: set[str], limit: int) -> list[dict]:
    """Query the DB for inbox threads to score, newest-first.

    Returns a list of dicts with keys: thread_id, gmail_thread_id, subject,
    sender, body_preview. Overfetches to account for excluded threads so the
    final pool is as close to `limit` as possible.

    Sorts by InboxThread.last_activity_at (the denormalized pointer
    inbox_repo.recompute_thread_pointers maintains and list_threads already
    sorts by) instead of the joined recent-message's gmail_internal_date —
    the two can diverge (e.g. a thread's most-recent message was soft-deleted
    and pointers recomputed) and last_activity_at is the source of truth
    everywhere else threads are ordered. Also excludes is_archived threads,
    matching list_threads' default view, so a candidate pool never scores
    threads the user no longer sees in their inbox. The join against
    InboxMessage is kept (not dropped) purely to pull from_addr/body_preview
    for the still-current recent_message_id. Shared by
    workers/task_engine_tasks.py's propose_task_draft (recency-pool
    fallback when its keyword probes come up empty) — the old
    draft_preview_bucket caller was deleted in Phase 4 Task 3.
    """
    stmt = (
        select(InboxThread.id, InboxThread.gmail_id, InboxThread.subject,
               InboxMessage.from_addr, InboxMessage.body_preview)
        .outerjoin(InboxMessage, InboxMessage.id == InboxThread.recent_message_id)
        .where(InboxThread.user_id == user_id, InboxThread.is_archived == False)  # noqa: E712
        .order_by(InboxThread.last_activity_at.desc().nulls_last())
        .limit(limit + len(exclude))  # fetch extra so excludes don't shrink the pool
    )
    out = []
    for row in db.execute(stmt).all():
        tid, gid, subject, sender, preview = row
        if tid in exclude:
            continue
        out.append({"thread_id": tid, "gmail_thread_id": gid, "subject": subject,
                    "sender": sender, "body_preview": preview})
        if len(out) >= limit:
            break
    return out


@celery_app.task(name="app.workers.tasks.extend_inbox_history")
def extend_inbox_history_task(user_id: str, before_internal_date_ms: int) -> None:
    """Pull older threads on demand for a user.

    Acquires the per-user sync_lock so it cannot race with a concurrent full
    or partial sync. Calls extend_inbox_history which issues gmail.threads.list
    with q=before:<unix-secs>, classifies+upserts each stub, and leaves
    gmail_last_history_id untouched (the cursor must stay anchored at the most-
    recent message so future partial syncs keep working). Publishes an
    extend_complete event with the list of internal thread ids and a 'more' flag
    that is True when Gmail returned the full page of 200 stubs (meaning there
    are likely even older threads available).

    Also enqueues process_task_updates for any thread extend_inbox_history
    freshly linked to a tracker (new_link_ids) — without this, a thread pulled
    in via "load more history" that matches a tracker would sit
    attached-but-never-extracted until an unrelated future sync happened to
    re-touch it.
    """
    if not sync_lock.acquire(user_id):
        log.info("extend_task: user=%s syncing already, skip", user_id)
        return
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return
        log.info("extend_task: user=%s starting before_ms=%d", user_id, before_internal_date_ms)
        ids, more, new_link_ids = gmail_sync.extend_inbox_history(
            db, user=user, before_internal_date_ms=before_internal_date_ms,
        )
        log.info("extend_task: user=%s upserted %d ids, more=%s; publishing", user_id, len(ids), more)
        _publish(user_id, "extend_complete", {"thread_ids": ids, "more": more})
        if new_link_ids:
            task_engine_tasks.process_task_updates.apply_async(args=[user_id, new_link_ids], countdown=0)
    finally:
        db.close()
        sync_lock.release(user_id)


def _score_all(db, *, user_id: str, candidates: list[dict], name: str, description: str) -> list[dict]:
    """Score candidates from Postgres bodies (0006+) in parallel under the
    shared LLM semaphore. The sequential gmail refetch is gone."""
    triples = inbox_repo.load_parsed_threads(
        db, user_id=user_id, internal_ids=[c["thread_id"] for c in candidates])
    parsed_by_id = {internal_id: parsed for internal_id, _, parsed in triples}
    pairs = [(c, parsed_by_id[c["thread_id"]]) for c in candidates
             if c["thread_id"] in parsed_by_id]

    s = get_settings()

    async def _score_one(parsed):
        text = await llm_client.call_messages(
            model=s.llm_classify_model,
            system=score_thread.SYSTEM_PROMPT,
            user=score_thread.build_user_message(
                thread_str=thread_to_string(parsed), name=name, description=description),
            stage="score", user_id=user_id,
        )
        return score_thread.parse_response(text)

    async def _all():
        return await asyncio.gather(*[_score_one(p) for _, p in pairs])

    parsed_results = llm_client.run_in_loop(_all())

    out = []
    for (c, _), result in zip(pairs, parsed_results):
        if not result:
            continue
        out.append({
            "thread_id": c["thread_id"], "subject": c["subject"], "sender": c["sender"],
            "score": result["score"], "rationale": result["rationale"],
            "snippet": result["snippet"],
        })
    return out
