"""Decoupled Celery module for task-engine extraction.

Kept separate from `workers/tasks.py` (Gmail sync) on purpose: extraction is
LLM-latency-bound and per-tracker fan-out, whereas sync is Gmail-API-bound —
mixing them onto one task module would make a slow extraction run block (or
compete with) the sync queue's own throughput.

`workers/tasks.py` imports this module at its own top level (the sync-enqueue
hook) — that means THIS module must never import `app.workers.tasks` at its
own top level, or the two modules would form an import cycle. `_publish`
(and, for `propose_task_draft` below, `_read_candidates`/`_score_all`/the
score thresholds) is therefore pulled in with a late import inside each task
function's body instead of a top-level `from app.workers.tasks import ...`.

No `sync_lock` anywhere in this module: no task here ever writes
`inbox_threads`/`inbox_messages` (they only read), so there's nothing that
can race the sync path's `(user_id, gmail_id)` unique constraint. Idempotency
for the two extraction tasks instead comes entirely from
`transitions.validate_and_stage`'s step 7 (SELECT-first check against
`(task_id, message_id, field)`, backed by the migrated DB's partial unique
index as a race backstop) — re-running either against the same (task,
thread) pair is always safe and produces no duplicate events.
`propose_task_draft` is read-only end to end (goal in, a cached draft +
one SSE push out) so idempotency isn't a concern for it at all — re-running
it against the same draft_id just overwrites the cache entry with a fresh
(possibly different) proposal.
"""

import logging

from app.config import get_settings
from app.db.models import User
from app.db.session import SessionLocal as _AppSessionLocal
from app.inbox import inbox_repo, search_repo
from app.llm import client as llm_client
from app.llm.prompts import propose_task
from app.task_engine import draft_cache
from app.task_engine import repo as task_repo
from app.task_engine import schema as schema_mod
from app.task_engine.engine import extract_for_pair
from app.workers.celery_app import celery_app

# Module-level seam so tests can rebind onto an in-memory engine, matching
# workers/tasks.py's convention.
SessionLocal = _AppSessionLocal
log = logging.getLogger(__name__)

# --- propose_task_draft constants ---
# Cap on unique candidate threads collected across all keyword_probes.
PROPOSE_CANDIDATE_CAP = 60
# Per-probe search_threads() limit (union'd + deduped up to the cap above).
PROPOSE_SEARCH_LIMIT_PER_PROBE = 20
# _read_candidates() pool size used when every probe comes up empty.
PROPOSE_READ_CANDIDATES_LIMIT = 40
# Top-N positives/near-misses surfaced in the draft payload (bucket drafts
# use 3 -- see workers/tasks.py's TOP_POSITIVES/TOP_NEAR_MISSES -- but this
# flow's own spec calls for 5).
PROPOSE_TOP_N = 5

# Fallback EPS schema used when the LLM can't produce a schema that survives
# one retry -- a minimal singleton tracker the user can still edit from the
# draft UI rather than seeing a hard failure.
_FALLBACK_SCHEMA_DICT = {
    "version": 1,
    "entity": None,
    "pipeline": {"stages": ["in_progress"], "terminal": ["done"]},
}

# Generic nudge appended to the retry when the FIRST propose attempt's
# response was not parseable JSON in the required shape at all (as opposed
# to parseable-but-schema-invalid, which gets the more specific
# validator-error nudge built inline where it's used). `parse_response`
# returning None is also what a transient LLM/API error degrades to --
# `llm_client.call_messages` returns "" on any error -- so this is the retry
# path a bare transient failure takes.
_UNPARSEABLE_RETRY_NUDGE = (
    "Your previous response was not a single line of valid JSON in the "
    "required shape. Respond again with exactly the JSON object described above."
)


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


def _llm_propose(*, goal: str, user_id: str, model: str, extra: str | None = None) -> dict | None:
    """One propose_task LLM round-trip -> shape-checked dict or None.

    `extra` is appended to the user message verbatim; the retry path below
    uses it to hand the model the exact validate_schema error message from
    the previous attempt."""
    user_message = propose_task.build_user_message(goal=goal)
    if extra:
        user_message = f"{user_message}\n\n{extra}"
    text = llm_client.run_in_loop(
        llm_client.call_messages(
            model=model, system=propose_task.SYSTEM_PROMPT, user=user_message,
            stage="propose", user_id=user_id,
        )
    )
    return propose_task.parse_response(text)


def _candidate_from_thread(db, *, user_id: str, thread) -> dict:
    """Adapt a `search_repo.search_threads()` InboxThread row into
    `tasks._score_all`'s candidate dict shape ({thread_id, gmail_thread_id,
    subject, sender, body_preview}). sender/body_preview aren't columns on
    InboxThread itself -- they come from the thread's recent message, same
    as `tasks._read_candidates`' outerjoin; a thread with no recent message
    (or a recent_message_id that fails to resolve) just yields None for
    both, matching that outerjoin's miss behavior."""
    sender = None
    body_preview = None
    if thread.recent_message_id:
        msg = inbox_repo.get_message(db, user_id=user_id, message_id=thread.recent_message_id)
        if msg is not None:
            sender = msg.from_addr
            body_preview = msg.body_preview
    return {
        "thread_id": thread.id, "gmail_thread_id": thread.gmail_id,
        "subject": thread.subject, "sender": sender, "body_preview": body_preview,
    }


@celery_app.task(name="app.workers.task_engine_tasks.propose_task_draft")
def propose_task_draft(user_id: str, draft_id: str, goal: str) -> None:
    """Goal -> proposed task draft: one Sonnet-class propose call, EPS
    validation, FTS-prefiltered candidate scoring, and a cache-then-publish
    so the modal's poll fallback always has somewhere to land.

    1. LLM propose (`propose_task.build_user_message`/`parse_response`).
       Exactly ONE retry total is ever spent per draft, no matter which of
       the two ways the first attempt can fail:
         - Unparseable (`parse_response` returned None -- no valid JSON in
           the required shape at all; this is also what a transient
           API/network error degrades to, since `llm_client.call_messages`
           returns "" on any error): retried once with a generic
           `_UNPARSEABLE_RETRY_NUDGE` appended to the user message.
         - Parseable but schema-invalid (shape-checked fine, but
           `schema_mod.validate_schema` raised ValueError on the
           `state_schema`): retried once with the validator's exact error
           message appended, so the model gets one shot at fixing precisely
           what it got wrong.
       Whichever branch fires first consumes the draft's one retry; the
       retry's response is checked once more and then we stop -- there is
       no second retry, so at most 2 LLM propose calls ever happen for one
       draft. In particular, if the first attempt was unparseable and the
       retry comes back parseable-but-schema-invalid, that schema failure
       does NOT get its own retry -- it falls straight through to the
       fallback schema below (this is the mixed case the naive
       "only the ValueError branch retries" version of this code used to
       miss: an unparseable first response used to fall straight to the
       fallback draft with zero retries at all).
       If the final (post-retry, if any) response's schema is invalid or
       never obtained, we give up on the LLM's schema and substitute the
       minimal singleton fallback -- the rest of the proposal (name/
       description/keyword_probes) still comes from whichever attempt
       actually returned a parseable response, so the user still gets a
       nameable, describable draft to edit rather than a total failure. Only
       if NEITHER attempt ever produced a parseable response do we degrade
       further to a bare name/description carved out of the goal itself and
       an empty probe list -- which naturally trips the probes-miss
       fallback in step 2 below rather than needing its own special case.
    2. Candidate examples: union of `search_repo.search_threads()` over the
       proposal's `keyword_probes` (cap PROPOSE_CANDIDATE_CAP unique threads
       across all probes, PROPOSE_SEARCH_LIMIT_PER_PROBE per probe) -- the
       FTS prefilter that keeps scoring cheap. When the probes are missing
       entirely or every one of them comes up empty, fall back to
       `tasks._read_candidates`' recency-ordered pool so the user isn't shown
       zero examples just because the LLM's search terms didn't land.
    3. Score every candidate against the proposed name/description with the
       EXISTING `tasks._score_all` -- the same 0-10 rubric bucket drafts use,
       just against a tracker's would-be name/description instead of a
       bucket's.
    4. Cache the result BEFORE publishing task_draft_ready (mirrors
       `draft_preview_bucket`'s cache-before-publish rationale in
       workers/tasks.py: a client polling GET .../draft/{draft_id} between
       the two must see the ready payload, never a stale "pending").

    `_publish`/`_read_candidates`/`_score_all`/the score thresholds are all
    late-imported from `app.workers.tasks` -- see this module's docstring for
    why (tasks.py imports task_engine_tasks at its own top level; importing
    tasks.py back at OUR top level would form a cycle).
    """
    from app.workers.tasks import (
        NEAR_MISS_HIGH, NEAR_MISS_LOW, POSITIVE_THRESHOLD,
        _publish, _read_candidates, _score_all,
    )

    log.info("propose_task_draft: user=%s draft=%s", user_id, draft_id)
    settings = get_settings()

    db = SessionLocal()
    try:
        # Parity with draft_preview_bucket: a draft request for a user who
        # no longer exists (or never did) must not spend an LLM call at all.
        user = db.get(User, user_id)
        if user is None:
            log.info(
                "propose_task_draft: draft=%s user=%s not found, skipping",
                draft_id, user_id,
            )
            return

        raw = _llm_propose(goal=goal, user_id=user_id, model=settings.llm_extract_model)
        retried = False

        if raw is None:
            log.info(
                "propose_task_draft: draft=%s first attempt unparseable; retrying once",
                draft_id,
            )
            raw = _llm_propose(
                goal=goal, user_id=user_id, model=settings.llm_extract_model,
                extra=_UNPARSEABLE_RETRY_NUDGE,
            )
            retried = True

        schema = None
        if raw is not None:
            try:
                schema = schema_mod.validate_schema(raw["state_schema"])
            except ValueError as exc:
                if retried:
                    # Already spent this draft's one retry on an unparseable
                    # first attempt -- do not spend a second one here; fall
                    # through to the fallback schema below instead.
                    log.info(
                        "propose_task_draft: draft=%s schema invalid on the "
                        "post-retry response (%s); retry budget spent, using "
                        "fallback schema",
                        draft_id, exc,
                    )
                else:
                    log.info(
                        "propose_task_draft: draft=%s schema invalid on first "
                        "attempt (%s); retrying once",
                        draft_id, exc,
                    )
                    retry_context = (
                        f"Your previous state_schema was invalid: {exc}\n"
                        "Fix it and return the full JSON object again."
                    )
                    raw2 = _llm_propose(
                        goal=goal, user_id=user_id, model=settings.llm_extract_model,
                        extra=retry_context,
                    )
                    if raw2 is not None:
                        raw = raw2
                        try:
                            schema = schema_mod.validate_schema(raw["state_schema"])
                        except ValueError as exc2:
                            log.info(
                                "propose_task_draft: draft=%s schema invalid on "
                                "retry too (%s); using fallback schema",
                                draft_id, exc2,
                            )

        if raw is None:
            # Neither attempt ever produced a parseable response -- degrade
            # to a bare proposal. keyword_probes=[] naturally trips the
            # probes-miss fallback in step 2 below, so no extra branching is
            # needed there.
            raw = {"name": goal[:40], "description": goal, "keyword_probes": []}

        if schema is None:
            schema = schema_mod.validate_schema(_FALLBACK_SCHEMA_DICT)

        probes = raw.get("keyword_probes") or []

        seen: set[str] = set()
        candidate_threads = []
        for probe in probes:
            if len(seen) >= PROPOSE_CANDIDATE_CAP:
                break
            for thread in search_repo.search_threads(
                db, user_id=user_id, q=probe, limit=PROPOSE_SEARCH_LIMIT_PER_PROBE,
            ):
                if thread.id in seen:
                    continue
                seen.add(thread.id)
                candidate_threads.append(thread)
                if len(seen) >= PROPOSE_CANDIDATE_CAP:
                    break

        if candidate_threads:
            candidates = [
                _candidate_from_thread(db, user_id=user_id, thread=t) for t in candidate_threads
            ]
        else:
            log.info(
                "propose_task_draft: draft=%s probes found nothing, falling back to recency pool",
                draft_id,
            )
            candidates = _read_candidates(
                db, user_id=user_id, exclude=set(), limit=PROPOSE_READ_CANDIDATES_LIMIT,
            )

        scored = _score_all(
            db, user_id=user_id, candidates=candidates,
            name=raw["name"], description=raw["description"],
        )

        positives = sorted(
            [s for s in scored if s["score"] >= POSITIVE_THRESHOLD],
            key=lambda s: -s["score"],
        )[:PROPOSE_TOP_N]
        near = sorted(
            [s for s in scored if NEAR_MISS_LOW <= s["score"] <= NEAR_MISS_HIGH],
            key=lambda s: -s["score"],
        )[:PROPOSE_TOP_N]

        payload = {
            "proposal": {
                "name": raw["name"],
                "description": raw["description"],
                "state_schema": schema.model_dump(),
                "keyword_probes": probes,
            },
            "positives": positives,
            "near_misses": near,
        }

        # Cache before publish -- see draft_preview_bucket's identical
        # rationale in workers/tasks.py.
        draft_cache.store_result(draft_id, user_id=user_id, payload=payload)
        _publish(user_id, "task_draft_ready", {"draft_id": draft_id})
    finally:
        db.close()
