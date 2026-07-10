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

No `sync_lock` anywhere in this module. Most tasks here only ever READ
`inbox_threads`/`inbox_messages`, so there's nothing that can race the sync
path's `(user_id, gmail_id)` unique constraint (the hazard sync_lock exists
to guard). Two exceptions are `_run_bucket_backfill` and `retriage_deleted_bucket`,
which DO write `InboxThread.bucket_id` — still without sync_lock, because
they do no Gmail I/O of their own and so never touch the unique-constraint
hazard the lock guards against. Those write paths have their own, narrower
race instead: a concurrent poll (holding sync_lock, re-triaging the same
thread against FRESH content) can commit a different bucket_id while a
backfill/retriage batch — which read the OLD bucket_id as its stability
hint before its own multi-second `classify.triage()` round-trip — is still
mid-flight. Both functions guard against this optimistically: immediately
before writing, they re-read `bucket_id` fresh (a column-level `select()`,
which always issues a real query rather than resolving from the session's
identity map) and skip the write — and the `threads_updated` publish — if
the row moved since the read that fed `triage()`. Idempotency for the two
extraction tasks instead comes entirely from
`transitions.validate_and_stage`'s step 7 (SELECT-first check against
`(task_id, message_id, field)`, backed by the migrated DB's partial unique
index as a race backstop) — re-running either against the same (task,
thread) pair is always safe and produces no duplicate events.
`propose_task_draft` writes only its own job row's `payload`/`stage`
(Phase 4.5 Task 3 — no more Redis draft_cache), so re-running it against the
same job_id just overwrites the row with a fresh (possibly different)
proposal — same "no duplicate side effects on replay" property, just a
Postgres row instead of a Redis cache entry.
"""

import logging

from sqlalchemy import select

from app.actions import engine as actions_engine
from app.config import get_settings
from app.db.models import InboxThread, User
from app.db.session import SessionLocal as _AppSessionLocal
from app.inbox import inbox_repo, search_repo
from app.llm import classify
from app.llm import client as llm_client
from app.llm.prompts import propose_task
from app.task_engine import jobs_repo
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
        from app.workers.tasks import _publish

        # Batch-resolved ONCE for the whole call (not per task/pair) so the
        # Phase 5 fire_rules_for_event hook below never N+1s a thread lookup
        # per applied event -- every pair this run might touch is a subset
        # of `touched`, known up front.
        threads_by_id = {
            t.id: t for t in inbox_repo.get_threads_batch(db, user_id=user_id, thread_ids=list(touched))
        }

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

                    # Phase 5 (actions, spec 006 §3): fire entity_entered_
                    # stage rules for every event this pair's extraction just
                    # applied, AFTER the commit above so a firing failure can
                    # never roll back the applied event itself.
                    if staged.applied:
                        thread_row = threads_by_id.get(thread_id)
                        if thread_row is not None:
                            for event in staged.applied:
                                actions_engine.fire_rules_for_event(
                                    db, user_id=user_id, task=task, event=event,
                                    thread_id=thread_id, gmail_thread_id=thread_row.gmail_id,
                                    publish=_publish,
                                )

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

        # Phase 5 (actions, spec 006 §3): same hook as process_task_updates,
        # for this single-pair variant.
        if staged.applied:
            thread_row = inbox_repo.get_thread(db, user_id=user_id, thread_id=thread_id)
            if thread_row is not None:
                from app.workers.tasks import _publish

                for event in staged.applied:
                    actions_engine.fire_rules_for_event(
                        db, user_id=user_id, task=task, event=event,
                        thread_id=thread_id, gmail_thread_id=thread_row.gmail_id,
                        publish=_publish,
                    )

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
def propose_task_draft(user_id: str, job_id: str, goal: str) -> None:
    """Goal -> proposed task draft: one Sonnet-class propose call, EPS
    validation, FTS-prefiltered candidate scoring, and a commit-then-publish
    write into the job row's `payload` (Phase 4.5 Task 3 -- replaces the
    old Redis draft_cache, whose 600s TTL could strand a client mid-review;
    a job row never expires).

    1. LLM propose (`propose_task.build_user_message`/`parse_response`).
       Exactly ONE retry total is ever spent per job, no matter which of
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
       Whichever branch fires first consumes the job's one retry; the
       retry's response is checked once more and then we stop -- there is
       no second retry, so at most 2 LLM propose calls ever happen for one
       job. In particular, if the first attempt was unparseable and the
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
       None of this counts as job failure -- a degraded-but-present draft
       still reaches `draft_ready` for the user to edit; see the top-level
       try/except below for what DOES mark the job `failed`.
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
    4. Write the proposal into the job row (`jobs_repo.set_payload` +
       `update_stage("draft_ready")`), commit, THEN publish `job_updated`
       -- commit-before-publish, same ordering rationale the retired
       draft_cache's cache-before-publish had: a client polling
       `GET /api/jobs/{job_id}` between the two must see the row already in
       `draft_ready`, never a stale `proposing`.

    A top-level try/except wraps this entire body (Phase 4.5 Task 3, mirrors
    `backfill_task`'s identical guard): ANY exception -- an LLM client bug, a
    DB error, anything -- marks the job `failed` with the error text (via
    `_record_job_failure`, on a fresh session -- see that function's
    docstring) and publishes `job_updated`, then re-raises so Celery still
    records the run as FAILED. Without this, a job whose worker crashed
    would sit in `proposing` forever with no signal to the user, the exact
    "stranded popup" failure mode this whole jobs surface exists to fix.

    `_publish`/`_read_candidates`/`_score_all`/the score thresholds are all
    late-imported from `app.workers.tasks` -- see this module's docstring for
    why (tasks.py imports task_engine_tasks at its own top level; importing
    tasks.py back at OUR top level would form a cycle).
    """
    from app.workers.tasks import (
        NEAR_MISS_HIGH, NEAR_MISS_LOW, POSITIVE_THRESHOLD,
        _publish, _read_candidates, _score_all,
    )

    log.info("propose_task_draft: user=%s job=%s", user_id, job_id)
    settings = get_settings()

    db = SessionLocal()
    try:
        # Parity with draft_preview_bucket: a draft request for a user who
        # no longer exists (or never did) must not spend an LLM call at all.
        user = db.get(User, user_id)
        if user is None:
            log.info(
                "propose_task_draft: job=%s user=%s not found, skipping",
                job_id, user_id,
            )
            return

        job = jobs_repo.get_owned_job(db, user_id=user_id, job_id=job_id)
        if job is None:
            log.warning(
                "propose_task_draft: job=%s not found for user=%s, skipping",
                job_id, user_id,
            )
            return

        raw = _llm_propose(goal=goal, user_id=user_id, model=settings.llm_extract_model)
        retried = False

        if raw is None:
            log.info(
                "propose_task_draft: job=%s first attempt unparseable; retrying once",
                job_id,
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
                    # Already spent this job's one retry on an unparseable
                    # first attempt -- do not spend a second one here; fall
                    # through to the fallback schema below instead.
                    log.info(
                        "propose_task_draft: job=%s schema invalid on the "
                        "post-retry response (%s); retry budget spent, using "
                        "fallback schema",
                        job_id, exc,
                    )
                else:
                    log.info(
                        "propose_task_draft: job=%s schema invalid on first "
                        "attempt (%s); retrying once",
                        job_id, exc,
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
                                "propose_task_draft: job=%s schema invalid on "
                                "retry too (%s); using fallback schema",
                                job_id, exc2,
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
                "propose_task_draft: job=%s probes found nothing, falling back to recency pool",
                job_id,
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

        jobs_repo.set_payload(db, job=job, payload=payload)
        jobs_repo.update_stage(db, job=job, stage="draft_ready")
        db.commit()
        _publish(user_id, "job_updated", {"job_id": job_id})
    except Exception as exc:
        log.exception("propose_task_draft: job=%s failed", job_id)
        # Guard rollback so it can't replace the original exception if connection drops.
        try:
            db.rollback()
        except Exception:
            log.exception("propose_task_draft: rollback failed; original error takes precedence")
        _record_job_failure(user_id=user_id, job_id=job_id, error=str(exc))
        raise
    finally:
        db.close()


# --- backfill_task constants ---
# Cap on unique candidate threads collected across all keyword_probes' FTS
# hits -- same shape as PROPOSE_CANDIDATE_CAP, just ~8x wider: backfill scans
# a brand-new tracker's entire history once at creation time (not a cheap
# draft preview), so a much wider net is worth the extra LLM calls.
BACKFILL_PROBE_CANDIDATE_CAP = 500
# Per-probe search_threads() limit feeding the cap above.
BACKFILL_SEARCH_LIMIT_PER_PROBE = 100
# Newest-N threads unioned on top of the FTS hits (include_archived=True --
# an archived thread is still task history, e.g. a job-application thread
# archived after applying), so a tracker whose keyword_probes underperform
# still sees recent traffic.
BACKFILL_RECENCY_LIMIT = 200
# Threads per classify.triage() call -- bounds each round-trip's asyncio
# fan-out. 25 evenly divides BACKFILL_PROGRESS_INTERVAL below, so a progress
# publish lands on a clean batch boundary whenever load_parsed_threads
# doesn't drop any candidate out of a batch.
BACKFILL_TRIAGE_BATCH = 25
# Publish task_backfill_progress after at least this many threads have been
# scanned since the last publish (a running remainder, not a strict multiple
# check -- see the loop below -- so a batch that lands off-boundary because
# load_parsed_threads dropped a thread still triggers a timely publish).
BACKFILL_PROGRESS_INTERVAL = 50


def _write_job_progress(
    db, *, job, scanned: int, matched: int, total: int, user_id: str, job_id: str, publish,
) -> None:
    """Shared per-batch job-row write for both backfill_task branches
    (Phase 4.5 Task 2): overwrite the progress counters, commit, and nudge
    the jobs panel. `total` is the fixed candidate-pool size for the whole
    run -- re-passing the same value on every call is equivalent to "written
    once" since it never changes between batches."""
    jobs_repo.update_progress(db, job=job, scanned=scanned, matched=matched, total=total)
    db.commit()
    publish(user_id, "job_updated", {"job_id": job_id})


def _write_job_done(db, *, job, user_id: str, job_id: str, publish) -> None:
    """Shared terminal job-row write for both backfill_task branches' happy
    path (Phase 4.5 Task 2). The failure path's equivalent write lives in
    `_record_job_failure` below, deliberately on a fresh session instead of
    this one -- see that function's docstring."""
    jobs_repo.update_stage(db, job=job, stage="done")
    db.commit()
    publish(user_id, "job_updated", {"job_id": job_id})


def _backfill_candidate_pool(db, *, user_id: str, keyword_probes: list[str] | None) -> list[str]:
    """Candidate thread ids for a backfill run -- shared by both of
    `backfill_task`'s kind branches. FTS union over `keyword_probes`
    (`search_repo.search_threads`, capped at BACKFILL_PROBE_CANDIDATE_CAP
    unique threads across all probes) unioned with the newest
    BACKFILL_RECENCY_LIMIT threads (`inbox_repo.list_threads(...,
    include_archived=True)`). Both sources use include_archived=True --
    backfill's whole point (tracker OR bucket) is to scan everything stored,
    not just what's currently in the live inbox view. Probes may be
    non-empty for a bucket backfill too -- POST /api/jobs/{id}/confirm
    forwards the draft's keyword_probes regardless of kind (the propose
    worker has no task_kind param, so it always generates probes; they drive
    the FTS prefilter for both kinds). The empty-probes case -- where the
    FTS half contributes nothing and this degrades to exactly the
    recency-window pool, the "empty probes -> recent-window fallback" some
    callers rely on -- comes from the legacy POST /api/buckets shim
    (hardcodes `keyword_probes=[]`) and direct POST /api/tasks creates
    (kind='bucket' with no probes supplied).
    """
    seen: set[str] = set()
    candidate_ids: list[str] = []
    for probe in (keyword_probes or []):
        if len(seen) >= BACKFILL_PROBE_CANDIDATE_CAP:
            break
        for thread in search_repo.search_threads(
            db, user_id=user_id, q=probe, include_archived=True,
            limit=BACKFILL_SEARCH_LIMIT_PER_PROBE,
        ):
            if thread.id in seen:
                continue
            seen.add(thread.id)
            candidate_ids.append(thread.id)
            if len(seen) >= BACKFILL_PROBE_CANDIDATE_CAP:
                break
    for thread in inbox_repo.list_threads(
        db, user_id=user_id, limit=BACKFILL_RECENCY_LIMIT, offset=0,
        include_archived=True,
    ):
        if thread.id in seen:
            continue
        seen.add(thread.id)
        candidate_ids.append(thread.id)
    return candidate_ids


def _run_tracker_backfill(
    db, *, task, user_id: str, candidate_ids: list[str], publish, job_id: str | None = None,
) -> None:
    """kind='tracker' backfill body -- unchanged Phase 2A/2C behavior, only
    extracted out of `backfill_task` so `_backfill_candidate_pool` can be
    shared with the kind='bucket' branch without duplicating it.

    Two phases, in order:

    1. Triage phase. Candidates (already resolved by
       `_backfill_candidate_pool`) are triaged in batches of
       BACKFILL_TRIAGE_BATCH via `classify.triage()`, called with ONLY this
       tracker in the tracker list, `buckets=[]`, and `task_id=task.id` (so
       each call's llm_calls metrics row is attributable to the tracker being
       backfilled). triage() always returns a bucket pick alongside tracker
       relevance, but that pick is entirely ignored here -- never written to
       thread.bucket_id or anywhere else. Backfill's only job here is tracker
       relevance; with buckets=[] the pick is always None anyway (see
       classify._triage_one's no-fit fallthrough), so there's nothing
       meaningful to write even if we wanted to.

       A candidate whose confidence for this one tracker clears
       `settings.task_link_confidence` is linked via
       `repo.upsert_link(..., origin="llm", state="attached")`. upsert_link's
       own sticky rule (an origin='user' row can never be downgraded by an
       origin='llm' call) is ALL the protection a user-detached thread needs
       here -- there is deliberately no extra "is this link user-owned?"
       guard in this function; upsert_link simply returns None for that row
       and this loop moves on, exactly as it would for any other no-op. A
       thread only counts toward `matched` (and the extraction phase below)
       when upsert_link actually returns a row.

    2. Extraction phase. Every thread this run actually attached is
       re-fetched and extracted via `extract_for_pair` in ASCENDING
       `last_activity_at` order -- chronological order matters here in a way
       it doesn't for `process_task_updates`' single-sync-tick batch: a fresh
       tracker's pipeline (e.g. todo -> in_progress -> done) must replay a
       thread's history oldest-first so later messages' transitions land on
       top of earlier ones, not the reverse. Each thread is extracted +
       committed independently, wrapped in its own try/except (log +
       rollback + continue, same idiom `process_task_updates` uses per-task)
       -- one thread's extraction blowing up must not abort the rest of a
       possibly long-running backfill.

    Progress: `task_backfill_progress` publishes every BACKFILL_PROGRESS_
    INTERVAL threads scanned during phase 1 (`{"task_id", "scanned",
    "matched", "done": False}`), a terminal one with `"done": True` once
    phase 2 finishes. `backfill_task` publishes the final `task_updated`
    itself, once this returns.

    `job_id` (optional, Phase 4.5 Task 2): when set, every triage batch
    below also overwrites the job row's `scanned`/`matched`/`total` counters
    (commit-then-publish `job_updated`, via `_write_job_progress`), and a
    final `stage='done'` write happens once phase 2 (extraction) finishes.
    This is a finer cadence than `task_backfill_progress`'s interval-gated
    publish above -- every batch, not every BACKFILL_PROGRESS_INTERVAL
    threads -- since the jobs panel's progress bar wants fresh numbers.
    """
    settings = get_settings()

    scanned = 0
    last_published = 0
    matched_thread_ids: set[str] = set()

    # Resolve the job row (if any) ONCE up front -- same session throughout,
    # so every per-batch write below reuses this one object rather than
    # re-querying it every iteration.
    job = jobs_repo.get_owned_job(db, user_id=user_id, job_id=job_id) if job_id is not None else None
    if job_id is not None and job is None:
        log.warning(
            "backfill_task: job=%s not found for user=%s; skipping job progress writes",
            job_id, user_id,
        )
    total = len(candidate_ids)

    for i in range(0, len(candidate_ids), BACKFILL_TRIAGE_BATCH):
        batch_ids = candidate_ids[i : i + BACKFILL_TRIAGE_BATCH]
        # load_parsed_threads may drop an id with no usable (non-deleted)
        # messages -- ordered_ids/parsed_list are derived from its
        # OWN returned triples (not batch_ids) so triage()'s
        # input-order-preserving output zips back up correctly.
        triples = inbox_repo.load_parsed_threads(db, user_id=user_id, internal_ids=batch_ids)
        if triples:
            ordered_ids = [tid for tid, _, _ in triples]
            parsed_list = [parsed for _, _, parsed in triples]
            gmail_id_by_thread = {tid: parsed.gmail_thread_id for tid, _, parsed in triples}
            results = classify.triage(
                parsed_list, buckets=[], trackers=[task],
                current_bucket_ids=[None] * len(parsed_list), user_id=user_id,
                task_id=task.id,
            )
            # Phase 5 (actions, spec 006 §3): freshly-attached links this
            # batch, collected for thread_linked rule firing right after the
            # batch's own commit below.
            fresh_links: list[tuple] = []
            for thread_id, (_, relevant_tasks) in zip(ordered_ids, results):
                if not relevant_tasks:
                    continue
                # Only this one tracker was passed in, so relevant_tasks
                # has at most one entry.
                _, confidence = relevant_tasks[0]
                if confidence < settings.task_link_confidence:
                    continue
                link_upsert = task_repo.upsert_link(
                    db, task_id=task.id, thread_id=thread_id, user_id=user_id,
                    origin="llm", state="attached", confidence=confidence,
                )
                if link_upsert.link is not None:
                    matched_thread_ids.add(thread_id)
                    if link_upsert.newly_attached:
                        fresh_links.append((link_upsert.link, thread_id, gmail_id_by_thread[thread_id]))
            db.commit()

            if fresh_links:
                for link, thread_id, gmail_thread_id in fresh_links:
                    actions_engine.fire_rules_for_link(
                        db, user_id=user_id, task=task, link=link,
                        thread_id=thread_id, gmail_thread_id=gmail_thread_id, publish=publish,
                    )

        # scanned tracks progress through the whole candidate pool,
        # including ids load_parsed_threads dropped -- those still
        # represent forward progress for the wizard's progress bar.
        scanned += len(batch_ids)
        if job is not None:
            _write_job_progress(
                db, job=job, scanned=scanned, matched=len(matched_thread_ids), total=total,
                user_id=user_id, job_id=job_id, publish=publish,
            )
        if scanned - last_published >= BACKFILL_PROGRESS_INTERVAL:
            publish(user_id, "task_backfill_progress", {
                "task_id": task.id, "scanned": scanned,
                "matched": len(matched_thread_ids), "done": False,
            })
            last_published = scanned

    # --- Extraction phase: ascending last_activity_at ---
    matched_threads = inbox_repo.get_threads_batch(
        db, user_id=user_id, thread_ids=list(matched_thread_ids),
    )
    matched_threads.sort(key=lambda t: (t.last_activity_at is None, t.last_activity_at))

    for thread in matched_threads:
        try:
            staged = extract_for_pair(
                db, task=task, thread_internal_id=thread.id, user_id=user_id,
            )
            if staged is None:
                continue
            db.commit()

            # Phase 5 (actions, spec 006 §3): same hook as
            # process_task_updates/extract_for_thread, for backfill's own
            # extraction phase.
            if staged.applied:
                for event in staged.applied:
                    actions_engine.fire_rules_for_event(
                        db, user_id=user_id, task=task, event=event,
                        thread_id=thread.id, gmail_thread_id=thread.gmail_id,
                        publish=publish,
                    )
        except Exception:
            log.exception(
                "backfill_task: task=%s thread=%s extraction failed; continuing",
                task.id, thread.id,
            )
            db.rollback()
            continue

    publish(user_id, "task_backfill_progress", {
        "task_id": task.id, "scanned": scanned,
        "matched": len(matched_thread_ids), "done": True,
    })
    if job is not None:
        _write_job_done(db, job=job, user_id=user_id, job_id=job_id, publish=publish)


def _run_bucket_backfill(
    db, *, task, user_id: str, candidate_ids: list[str], publish, publish_thread_ids,
    job_id: str | None = None,
) -> None:
    """kind='bucket' backfill body (Phase 4 Task 2): a pure reclassification
    pass over the candidate pool, no task-engine writes at all.

    Each batch is triaged against the FULL active bucket set
    (`repo.list_active_buckets` -- the new bucket included) with `trackers=[]`
    and each thread's CURRENT `bucket_id` passed as the stability hint via
    `current_bucket_ids` -- classify.triage() only returns a different pick
    when the thread genuinely fits the new bucket set better, exactly the
    stability rationale the old (Phase 4 Task 1) `workers.tasks._reclassify_
    all` used. `thread.bucket_id` is written only when the picked bucket
    differs from the stored one -- mirroring that same write discipline
    exactly. Unlike the tracker branch: no `repo.upsert_link` calls (bucket-
    kind tasks never get task_thread_links), no `extract_for_pair` calls, no
    task_events -- bucket-kind tasks track no board/pipeline state for
    extraction to write into.

    Progress cadence matches the tracker branch (interval `task_backfill_
    progress` publishes with `"done": False`, one terminal publish with
    `"done": True`) with "matched" reinterpreted as "bucket_id actually
    changed" -- the bucket-kind analog of the tracker branch's "newly
    linked". Every thread whose bucket changed is also published as a single
    `threads_updated` event (via `publish_thread_ids`, a no-op for an empty
    list) right before the terminal progress publish, so the browser's inbox
    view picks up the reassignment -- there is no final `task_updated` here
    (unlike the tracker branch): a bucket-kind task carries no version-gated
    board/event state for a client to refetch.

    `buckets` (the active bucket set triage() is called against) is resolved
    ONCE before the batch loop, not per-batch -- a bucket created or deleted
    mid-backfill is simply not seen by later batches. Acceptable: the backfill
    triages against the set as of its start, the same read-once semantics the
    old (Phase 4 Task 1) reclassify-all pass had.

    Write-time race: this function writes `InboxThread.bucket_id` without
    `sync_lock` (see the module docstring). A concurrent poll can commit a
    fresher pick for a thread already read into this batch's stability hint
    (`old_bucket`) before this batch's own `classify.triage()` call returns.
    Immediately before writing, each candidate whose pick differs from
    `old_bucket` is re-checked against a fresh, identity-map-bypassing
    `select(InboxThread.bucket_id)` -- if the stored value has already moved
    off `old_bucket`, something fresher won the race; skip the write (and
    leave the thread out of `changed_thread_ids`/the `threads_updated`
    publish) rather than clobber it with a pick computed from stale content.
    """
    scanned = 0
    last_published = 0
    changed_thread_ids: set[str] = set()
    buckets = task_repo.list_active_buckets(db, user_id=user_id)

    # Phase 4.5 Task 2 -- see _run_tracker_backfill's identical comment.
    job = jobs_repo.get_owned_job(db, user_id=user_id, job_id=job_id) if job_id is not None else None
    if job_id is not None and job is None:
        log.warning(
            "backfill_task: job=%s not found for user=%s; skipping job progress writes",
            job_id, user_id,
        )
    total = len(candidate_ids)

    for i in range(0, len(candidate_ids), BACKFILL_TRIAGE_BATCH):
        batch_ids = candidate_ids[i : i + BACKFILL_TRIAGE_BATCH]
        triples = inbox_repo.load_parsed_threads(db, user_id=user_id, internal_ids=batch_ids)
        if triples:
            ordered_ids = [tid for tid, _, _ in triples]
            current_bucket_ids = [bid for _, bid, _ in triples]
            parsed_list = [parsed for _, _, parsed in triples]
            results = classify.triage(
                parsed_list, buckets=buckets, trackers=[],
                current_bucket_ids=current_bucket_ids, user_id=user_id,
                task_id=task.id,
            )
            for thread_id, old_bucket, (new_bucket, _relevant_tasks) in zip(
                ordered_ids, current_bucket_ids, results,
            ):
                if new_bucket == old_bucket:
                    continue
                # Optimistic guard: a plain column select always issues a
                # real query (unlike db.get(), which would return this
                # session's identity-mapped InboxThread -- loaded by
                # load_parsed_threads above, before triage()'s multi-second
                # round-trip -- and so could still report the stale
                # old_bucket even though the row has since moved).
                fresh_bucket = db.execute(
                    select(InboxThread.bucket_id).where(InboxThread.id == thread_id)
                ).scalar_one_or_none()
                if fresh_bucket != old_bucket:
                    log.info(
                        "_run_bucket_backfill: task=%s thread=%s bucket_id moved "
                        "(%r -> %r) since triage read; skipping stale write",
                        task.id, thread_id, old_bucket, fresh_bucket,
                    )
                    continue
                thread_row = db.get(InboxThread, thread_id)
                if thread_row is not None:
                    thread_row.bucket_id = new_bucket
                    changed_thread_ids.add(thread_id)
            db.commit()

        scanned += len(batch_ids)
        if job is not None:
            _write_job_progress(
                db, job=job, scanned=scanned, matched=len(changed_thread_ids), total=total,
                user_id=user_id, job_id=job_id, publish=publish,
            )
        if scanned - last_published >= BACKFILL_PROGRESS_INTERVAL:
            publish(user_id, "task_backfill_progress", {
                "task_id": task.id, "scanned": scanned,
                "matched": len(changed_thread_ids), "done": False,
            })
            last_published = scanned

    publish_thread_ids(user_id, list(changed_thread_ids))
    publish(user_id, "task_backfill_progress", {
        "task_id": task.id, "scanned": scanned,
        "matched": len(changed_thread_ids), "done": True,
    })
    if job is not None:
        _write_job_done(db, job=job, user_id=user_id, job_id=job_id, publish=publish)


def _record_job_failure(*, user_id: str, job_id: str, error: str) -> None:
    """Best-effort terminal failure write, shared by `backfill_task`'s (Phase
    4.5 Task 2) and `propose_task_draft`'s (Task 3) top-level except clauses.
    Deliberately opens a BRAND-NEW session
    rather than reusing the run's own `db` -- that session may be poisoned
    by whatever just raised (a failed mid-batch commit, a dropped
    connection, an error thrown from deep inside triage/extraction with the
    session left in an unknown state), and this write's only job is to make
    the failure visible no matter what broke. Any failure in this write
    itself is logged and swallowed, never re-raised -- the caller's `raise`
    must surface the ORIGINAL exception, not a secondary one from this
    best-effort path. Late-imports `_publish` for the same import-cycle
    reason the rest of this module does (see module docstring)."""
    from app.workers.tasks import _publish

    fail_db = SessionLocal()
    try:
        job = jobs_repo.get_owned_job(fail_db, user_id=user_id, job_id=job_id)
        if job is None:
            log.warning(
                "backfill_task: job=%s not found for user=%s while recording failure",
                job_id, user_id,
            )
            return
        jobs_repo.mark_failed(fail_db, job=job, error=error)
        fail_db.commit()
        _publish(user_id, "job_updated", {"job_id": job_id})
    except Exception:
        log.exception(
            "backfill_task: failed to record job failure for job=%s user=%s", job_id, user_id,
        )
    finally:
        fail_db.close()


@celery_app.task(name="app.workers.task_engine_tasks.retriage_deleted_bucket")
def retriage_deleted_bucket(user_id: str, job_id: str, deleted_task_id: str) -> None:
    """Re-triage the threads orphaned by deleting a bucket-kind task (Phase
    4.5 Task 4, spec 005 §1.5) -- enqueued by `api/tasks.py`'s `delete_task`
    route immediately after the soft-delete's own commit, and only when that
    bucket still had >=1 thread pointing at it. Unlike `backfill_task`,
    `job_id` here is never optional -- the caller always creates the job row
    before enqueuing this task, so a missing job row means something is
    already wrong (see the defensive early-return below), not an intentional
    "run without job tracking" mode.

    The thread set is derived fresh at run start with a plain `select` over
    `(user_id, bucket_id=deleted_task_id)` -- no FTS/candidate-pool resolver
    the way `_backfill_candidate_pool` needs, since every candidate here is
    already known by construction: it IS the orphaned set, not something to
    search for. A small window may have passed since the delete route's own
    COUNT query, so this is a fresh read. On the first batch, `_write_job_progress`
    self-reconciles the job's `total` with this run's fresh count (overwriting
    the API-time count); only a fully-empty candidate set leaves that original
    total untouched. `scanned`/`matched` below are computed against THIS run's own set.

    Each `BACKFILL_TRIAGE_BATCH` batch is triaged against `list_active_
    buckets` (resolved once, up front -- same read-once semantics
    `_run_bucket_backfill` uses) with `trackers=[]` and each thread's OWN
    bucket_id (== `deleted_task_id`, by construction, modulo the same kind of
    race `_run_bucket_backfill` already tolerates) as the stability hint.
    `classify.triage()`'s no-fit fallback (`_triage_one`: `bucket_id =
    current_bucket_id` when `parse_response` found no match) therefore falls
    back to the DELETED bucket's own id, not a live one -- `list_active_
    buckets` already excludes it (its `is_deleted` flag was flipped by the
    caller one commit before this task was even enqueued), so there is
    nothing extra to filter here. That fallback pick, and a bare `None`
    (only reachable if `list_active_buckets` comes back empty -- `triage()`'s
    own empty-buckets-and-trackers short-circuit returns `(None, [])` for
    every thread without even calling the LLM), both resolve to `bucket_id=
    NULL` (unclassified) -- see the inline `resolved =` line below. Any OTHER
    pick is a genuine live bucket and gets written as-is. `matched` counts
    only threads that landed in a REAL bucket (`resolved is not None`) --
    every surviving orphan here "changes" by construction (its stored value
    was the now-dead bucket, which `resolved` can never reproduce), so
    `changed` (the `threads_updated` publish set) is the broader of the two.

    Write discipline mirrors `_run_bucket_backfill` exactly: write only when
    `resolved` differs from the thread's own stability-hint value, guarded by
    the same optimistic re-read (a fresh, identity-map-bypassing `select
    (InboxThread.bucket_id)`) immediately before writing -- a concurrent poll
    that already moved the thread off its stability hint wins, and this write
    (and that thread's inclusion in the `threads_updated` publish) is
    skipped.

    Progress/completion signals: per-batch `_write_job_progress` (commit-
    then-publish `job_updated`), a `threads_updated` publish for every
    rewritten thread once the loop finishes, then a terminal `job_updated`
    via `_write_job_done` -- no `task_backfill_progress`/`task_updated` here
    at all (there is no live task-engine board/event state on a bucket-kind
    task, deleted or not, for a client to refetch via either signal).

    A top-level try/except mirrors `backfill_task`'s: any exception marks the
    job `failed` (via `_record_job_failure`, on a FRESH session -- see that
    function's docstring) and re-raises so Celery still records the run as
    FAILED.
    """
    from app.workers.tasks import _publish, _publish_thread_ids

    db = SessionLocal()
    try:
        job = jobs_repo.get_owned_job(db, user_id=user_id, job_id=job_id)
        if job is None:
            log.warning(
                "retriage_deleted_bucket: job=%s not found for user=%s, skipping",
                job_id, user_id,
            )
            return

        candidate_ids = [
            row[0] for row in db.execute(
                select(InboxThread.id).where(
                    InboxThread.user_id == user_id,
                    InboxThread.bucket_id == deleted_task_id,
                )
            ).all()
        ]
        buckets = task_repo.list_active_buckets(db, user_id=user_id)
        total = len(candidate_ids)

        scanned = 0
        matched_thread_ids: set[str] = set()
        changed_thread_ids: set[str] = set()

        for i in range(0, len(candidate_ids), BACKFILL_TRIAGE_BATCH):
            batch_ids = candidate_ids[i : i + BACKFILL_TRIAGE_BATCH]
            triples = inbox_repo.load_parsed_threads(db, user_id=user_id, internal_ids=batch_ids)
            if triples:
                ordered_ids = [tid for tid, _, _ in triples]
                current_bucket_ids = [bid for _, bid, _ in triples]
                parsed_list = [parsed for _, _, parsed in triples]
                results = classify.triage(
                    parsed_list, buckets=buckets, trackers=[],
                    current_bucket_ids=current_bucket_ids, user_id=user_id,
                    task_id=deleted_task_id,
                )
                for thread_id, old_bucket, (pick, _relevant_tasks) in zip(
                    ordered_ids, current_bucket_ids, results,
                ):
                    # The deleted bucket is never a valid destination -- a
                    # pick that stayed there (the no-fit fallback) or a bare
                    # None (empty active-bucket set) both mean "unclassified".
                    resolved = None if pick in (deleted_task_id, None) else pick
                    if resolved == old_bucket:
                        continue
                    fresh_bucket = db.execute(
                        select(InboxThread.bucket_id).where(InboxThread.id == thread_id)
                    ).scalar_one_or_none()
                    if fresh_bucket != old_bucket:
                        log.info(
                            "retriage_deleted_bucket: job=%s thread=%s bucket_id moved "
                            "(%r -> %r) since triage read; skipping stale write",
                            job_id, thread_id, old_bucket, fresh_bucket,
                        )
                        continue
                    thread_row = db.get(InboxThread, thread_id)
                    if thread_row is not None:
                        thread_row.bucket_id = resolved
                        changed_thread_ids.add(thread_id)
                        if resolved is not None:
                            matched_thread_ids.add(thread_id)
                db.commit()

            scanned += len(batch_ids)
            _write_job_progress(
                db, job=job, scanned=scanned, matched=len(matched_thread_ids), total=total,
                user_id=user_id, job_id=job_id, publish=_publish,
            )

        _publish_thread_ids(user_id, list(changed_thread_ids))
        _write_job_done(db, job=job, user_id=user_id, job_id=job_id, publish=_publish)
    except Exception as exc:
        log.exception("retriage_deleted_bucket: job=%s failed", job_id)
        # Guard rollback so it can't replace the original exception if connection drops.
        try:
            db.rollback()
        except Exception:
            log.exception("retriage_deleted_bucket: rollback failed; original error takes precedence")
        _record_job_failure(user_id=user_id, job_id=job_id, error=str(exc))
        raise
    finally:
        db.close()


@celery_app.task(name="app.workers.task_engine_tasks.backfill_task")
def backfill_task(
    user_id: str, task_id: str, keyword_probes: list[str] | None = None,
    job_id: str | None = None,
) -> None:
    """Run a newly created task -- tracker OR bucket kind -- over a user's
    stored history.

    Both kinds resolve the same candidate pool up front
    (`_backfill_candidate_pool`) and share the BACKFILL_TRIAGE_BATCH/
    BACKFILL_PROGRESS_INTERVAL cadence, then diverge entirely:

    - kind='tracker' (`_run_tracker_backfill`, unchanged Phase 2A/2C
      behavior): triage against ONLY this tracker, link every thread that
      clears `settings.task_link_confidence`, then extract every newly-
      linked thread in ascending `last_activity_at` order. Followed by
      exactly one final `task_updated` (fresh version + pending_count).
    - kind='bucket' (`_run_bucket_backfill`, Phase 4 Task 2): triage against
      the FULL active bucket set with each thread's current `bucket_id` as
      the stability hint, writing `thread.bucket_id` only when the pick
      actually changes -- a pure reclassification pass with NO task_thread_
      links, NO task_events, and NO extraction. Publishes `threads_updated`
      for the changed threads instead of a final `task_updated` (there is no
      task-engine board/event state on a bucket-kind task for a client to
      refetch).

    Both kinds are idempotent by construction: re-running this task for the
    same task_id re-derives the same candidate pool and re-triages it, so a
    second run changes nothing new (tracker: `upsert_link` no-ops +
    extraction's own SELECT-first idempotency check; bucket: the pick is
    re-derived against the now-updated `bucket_id`, so an already-converged
    thread's pick no longer differs and nothing is rewritten) -- both still
    publish progress + a terminal signal on every run, since backfill is a
    one-shot job the wizard waits on and always reports a definitive
    completion, unlike `process_task_updates`' skip-if-no-change publish
    suppression for a periodic sync tick.

    `job_id` (optional, Phase 4.5 Task 2 -- jobs surface): when the caller
    threads a job row's id through (today, only the jobs-surface confirm
    endpoint does; a direct `POST /api/tasks` passes none), both branches
    write per-batch progress into that job row (`scanned`/`matched`/`total`,
    commit-then-publish `job_updated`) and mark it `stage='done'` on their
    own terminal write -- entirely additional to their unchanged
    `task_backfill_progress`/`task_updated` publishes. `job_id=None` (every
    pre-existing caller) takes none of these extra reads/writes at all, so
    its behavior is unchanged byte-for-byte.

    A TOP-LEVEL `try/except` wraps the entire body (both kinds): if ANYTHING
    raises -- an LLM failure, a DB error, a bug in either branch -- the run's
    own session is rolled back and, when `job_id` is set, the job row is
    marked `stage='failed'` with the exception text (via
    `_record_job_failure`, on a FRESH session -- see that function's
    docstring for why) and a `job_updated` nudge is published, before the
    exception is re-raised so Celery still records the run as FAILED. This
    closes the ledgered latent where a backfill that raised before its
    terminal publish left a client polling forever with no signal anything
    went wrong.
    """
    from app.workers.tasks import _publish, _publish_thread_ids

    db = SessionLocal()
    try:
        task = task_repo.get_owned_task(db, user_id=user_id, task_id=task_id)
        if task is None or task.status != "active":
            log.info("backfill_task: task=%s not an active task, skipping", task_id)
            return
        if task.kind == "tracker" and task.state_schema is None:
            log.info(
                "backfill_task: task=%s tracker has no state_schema yet, skipping", task_id,
            )
            return
        if task.kind not in ("tracker", "bucket"):
            log.info("backfill_task: task=%s unsupported kind=%r, skipping", task_id, task.kind)
            return

        candidate_ids = _backfill_candidate_pool(db, user_id=user_id, keyword_probes=keyword_probes)

        if task.kind == "bucket":
            _run_bucket_backfill(
                db, task=task, user_id=user_id, candidate_ids=candidate_ids,
                publish=_publish, publish_thread_ids=_publish_thread_ids, job_id=job_id,
            )
            return

        _run_tracker_backfill(
            db, task=task, user_id=user_id, candidate_ids=candidate_ids, publish=_publish,
            job_id=job_id,
        )
        _publish_task_updated(db, user_id=user_id, task=task)
    except Exception as exc:
        log.exception("backfill_task: task=%s failed", task_id)
        # Guard rollback so it can't replace the original exception if connection drops.
        try:
            db.rollback()
        except Exception:
            log.exception("backfill_task: rollback failed; original error takes precedence")
        if job_id is not None:
            _record_job_failure(user_id=user_id, job_id=job_id, error=str(exc))
        raise
    finally:
        db.close()
