"""Phase 5 (actions, spec 006 §3) execution worker: `execute_action(user_id,
action_id)` — the Celery entrypoint a mode='auto' rule fire enqueues (via
app.actions.engine.fire_rules_for_link/fire_rules_for_event), and
`execute_action_inner` — the shared synchronous seam a future approve route
(rules CRUD API) calls directly (no Celery round-trip) so a user clicking
"approve" on a mode='propose' action gets the result inline, through the
exact same dispatch code this module's own Celery task uses.

Kept as its own module (not folded into workers/action_tasks living inside
task_engine_tasks.py or actions/engine.py) so it can import app.gmail.client
and app.llm.prompts.draft_reply without either of those needing to know
anything about the rule-firing engine that enqueues it.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.actions import repo as actions_repo
from app.config import get_settings
from app.db.models import Task, TaskAction, TaskEvent, TaskThreadLink, User
from app.db.session import SessionLocal as _AppSessionLocal
from app.gmail import client as gmail_client
from app.gmail.parser import thread_to_string
from app.inbox import inbox_repo
from app.llm import client as llm_client
from app.llm.prompts import draft_reply as draft_reply_prompt
from app.workers.celery_app import celery_app

# Module-level seam so tests can rebind onto an in-memory engine, matching
# workers/tasks.py and workers/task_engine_tasks.py's own convention.
SessionLocal = _AppSessionLocal
log = logging.getLogger(__name__)


def _source_still_valid(db: Session, *, action: TaskAction) -> bool:
    """Re-check the action's evidence hasn't been reverted/detached since it
    was proposed (spec §6 invariant 7's belt-and-suspenders half —
    revert_event/detach_thread already auto-reject a still-'proposed' action
    synchronously, in the SAME transaction as the revert/detach itself; this
    is the re-check for the window where execute_action runs later,
    asynchronously, after an intervening revert/detach already committed
    before this worker even picked the job up)."""
    if action.source_event_id is not None:
        event = db.get(TaskEvent, action.source_event_id)
        return event is not None and event.status == "applied"
    link = db.get(TaskThreadLink, action.source_link_id)
    return link is not None and link.state == "attached"


def _load_thread_text(db: Session, *, user_id: str, thread_id: str | None) -> str | None:
    """Rebuild the thread's text from stored Postgres rows for the
    draft_reply prompt — never refetches Gmail, same self-sufficiency rule
    task_engine/engine.py's extract_for_pair follows. None (caller treats as
    a failure) when thread_id is the nullable soft-pointer's None, or the
    thread has no usable (non-deleted) rows left."""
    if thread_id is None:
        return None
    triples = inbox_repo.load_parsed_threads(db, user_id=user_id, internal_ids=[thread_id])
    if not triples:
        return None
    _, _, parsed = triples[0]
    return thread_to_string(parsed)


def _execute_draft_reply(db: Session, *, user: User, task: Task | None, action: TaskAction) -> dict:
    """draft_reply's dispatch branch: build the prompt (task goal + rule
    instructions frozen onto the action + thread text from Postgres + the
    source event's verbatim evidence quote, when this action was event-
    triggered), call the LLM once (stage='action', the EXTRACT model
    setting), then create the Gmail draft with the model's plain-text
    response as the body. Raises RuntimeError if the thread's content is no
    longer available — treated by the caller like any other unexpected
    dispatch failure, not a controlled 'failed' outcome, since a thread
    vanishing out from under a still-proposed action is not a scope/
    permission problem."""
    thread_text = _load_thread_text(db, user_id=user.id, thread_id=action.thread_id)
    if thread_text is None:
        raise RuntimeError(f"action {action.id}: thread content unavailable for draft_reply")

    instructions = (action.action_params or {}).get("instructions", "")
    evidence_quote = None
    if action.source_event_id is not None:
        source_event = db.get(TaskEvent, action.source_event_id)
        evidence_quote = source_event.evidence_quote if source_event is not None else None

    settings = get_settings()
    user_message = draft_reply_prompt.build_user_message(
        goal=task.goal if task is not None else "",
        instructions=instructions, thread_text=thread_text, evidence_quote=evidence_quote,
    )
    body_text = llm_client.run_in_loop(
        llm_client.call_messages(
            model=settings.llm_extract_model, system=draft_reply_prompt.SYSTEM_PROMPT,
            user=user_message, stage="action", user_id=user.id, task_id=action.task_id,
        )
    )
    # Defensive: llm_client.call_messages returns "" on API/network errors instead of raising,
    # so an empty body would flow silently into create_draft, producing a blank Gmail draft
    # marked status='executed'. Fail loudly instead, routing through the existing
    # unexpected-exception path (guarded rollback → fresh-session failed write → re-raise).
    if not body_text.strip():
        raise RuntimeError("draft_reply: LLM returned an empty draft body (see llm_calls for the underlying error)")
    return gmail_client.create_draft(db, user, action.gmail_thread_id, body_text)


def execute_action_inner(db: Session, *, user: User, action: TaskAction, publish) -> None:
    """The dispatch + status transition, given an already-loaded, already-
    owner-scoped, already-status-checked ('proposed') action. Precondition
    checks (not found / wrong status) are each caller's own job — a Celery
    task logs + returns, a future HTTP route 404s/409s — so this function
    only does the work that's identical either way.

    Re-checks source validity (see _source_still_valid) -> 'rejected' if the
    evidence is gone. Dispatches by action_type to the T2 Gmail write client;
    a MissingScopesError from any of those (raised BEFORE any network call)
    is a controlled, expected outcome -> 'failed' with "needs permission:
    ...". Any OTHER exception during dispatch propagates OUT of this
    function uncaught — each caller owns its own crash-handling policy
    (execute_action's Celery wrapper below mirrors workers/task_engine_tasks
    .py's _record_job_failure discipline; a synchronous HTTP caller would
    translate differently).

    Success -> status='executed', result=the dispatched method's dict,
    executed_at=now, commit, publish `action_updated`.
    """
    if not _source_still_valid(db, action=action):
        actions_repo.set_status(db, action=action, status="rejected")
        db.commit()
        publish(user.id, "action_updated", {"task_id": action.task_id})
        return

    task = db.get(Task, action.task_id)

    try:
        if action.action_type == "archive_thread":
            result = gmail_client.archive_thread(db, user, action.gmail_thread_id)
        elif action.action_type == "label_thread":
            label = (action.action_params or {}).get("label", "")
            result = gmail_client.label_thread(db, user, action.gmail_thread_id, label)
        elif action.action_type == "draft_reply":
            result = _execute_draft_reply(db, user=user, task=task, action=action)
        else:
            raise ValueError(f"unknown action_type {action.action_type!r}")
    except gmail_client.MissingScopesError as exc:
        actions_repo.set_status(db, action=action, status="failed", error=str(exc))
        db.commit()
        publish(user.id, "action_updated", {"task_id": action.task_id})
        return

    actions_repo.set_status(
        db, action=action, status="executed", result=result,
        executed_at=datetime.now(timezone.utc),
    )
    db.commit()
    publish(user.id, "action_updated", {"task_id": action.task_id})


def _record_action_failure(*, user_id: str, action_id: str, error: str) -> None:
    """Best-effort terminal failure write, mirroring workers/
    task_engine_tasks.py's _record_job_failure exactly: a BRAND-NEW session
    (the run's own `db` may be poisoned by whatever just raised), swallows
    its own exceptions (a secondary failure here must never mask the
    original the caller's `raise` needs to surface), late-imports _publish
    for the same import-cycle reason the rest of the workers/ package
    does."""
    from app.workers.tasks import _publish

    fail_db = SessionLocal()
    try:
        action = actions_repo.get_owned_action(fail_db, user_id=user_id, action_id=action_id)
        if action is None:
            log.warning(
                "execute_action: action=%s not found for user=%s while recording failure",
                action_id, user_id,
            )
            return
        actions_repo.set_status(fail_db, action=action, status="failed", error=error)
        fail_db.commit()
        _publish(user_id, "action_updated", {"task_id": action.task_id})
    except Exception:
        log.exception(
            "execute_action: failed to record action failure for action=%s user=%s",
            action_id, user_id,
        )
    finally:
        fail_db.close()


@celery_app.task(name="app.workers.action_tasks.execute_action")
def execute_action(user_id: str, action_id: str) -> None:
    """Celery entrypoint — ONLY ever enqueued by app.actions.engine's
    mode='auto' dispatch (fire_rules_for_event/fire_rules_for_link). A
    future approve route (rules CRUD API) calls execute_action_inner
    directly instead, bypassing this task entirely, since approving a
    mode='propose' action is a synchronous user action, not something to
    round-trip through Celery.
    """
    from app.workers.tasks import _publish

    db = SessionLocal()
    try:
        action = actions_repo.get_owned_action(db, user_id=user_id, action_id=action_id)
        if action is None:
            log.warning("execute_action: action=%s not found for user=%s, skipping", action_id, user_id)
            return
        if action.status != "proposed":
            log.info(
                "execute_action: action=%s status=%s, not proposed; skipping",
                action_id, action.status,
            )
            return
        if action.action_type == "draft_reply":
            # Belt + suspenders (spec §6 invariant 2): this Celery task is
            # only ever enqueued by a mode='auto' rule fire, and draft_reply
            # rules can never be mode='auto' (rejected at rule-write time by
            # the rules CRUD API, and asserted again in actions/engine.py's
            # dispatch) -- reaching this branch means one of those guards was
            # somehow bypassed. Refuse rather than silently drafting an
            # unreviewed reply outside the approve flow.
            log.error(
                "execute_action: action=%s is draft_reply but reached the auto-only "
                "Celery entrypoint; refusing (draft_reply must go through approve)",
                action_id,
            )
            actions_repo.set_status(db, action=action, status="failed", error="draft_reply cannot auto-execute")
            db.commit()
            _publish(user_id, "action_updated", {"task_id": action.task_id})
            return

        user = db.get(User, user_id)
        if user is None:
            log.warning("execute_action: user=%s not found, skipping action=%s", user_id, action_id)
            return
        execute_action_inner(db, user=user, action=action, publish=_publish)
    except Exception as exc:
        log.exception("execute_action: action=%s failed", action_id)
        # Guard rollback so it can't replace the original exception if connection drops.
        try:
            db.rollback()
        except Exception:
            log.exception("execute_action: rollback failed; original error takes precedence")
        _record_action_failure(user_id=user_id, action_id=action_id, error=str(exc))
        raise
    finally:
        db.close()
