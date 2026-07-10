"""Phase 5 (actions, spec 006 §3) rule-firing engine.

Bridges the pure evaluator (`app.actions.rules.evaluate_event/evaluate_link`)
and the audit-ledger CRUD (`app.actions.repo`) to the two kinds of firing
evidence: a just-applied `TaskEvent`, or a freshly-attached `TaskThreadLink`.
Lives in its own module — not `workers/task_engine_tasks.py`, not
`workers/gmail_sync.py`, not `api/tasks.py` — so all three callers can import
it without an import cycle: this module depends only on `app.actions.{repo,
rules}` and `app.db.models`, never on any `workers/`, `api/`, or `gmail/`
module, so nothing importing IT can cycle back.

CALLER CONTRACT: `fire_rules_for_event`/`fire_rules_for_link` each COMMIT
their own transaction (the TaskAction inserts). Callers MUST invoke them
only AFTER their own commit of the event/link that is the firing evidence
— never before — so a failure inside this module (a bug, a constraint
violation) can never roll back work the caller already durably committed.
This is one level past workers/task_engine_tasks.py's own publish-after-
commit discipline: commit (caller) -> commit (this module) -> publish.
"""

import logging

from sqlalchemy.orm import Session

from app.actions import repo as actions_repo
from app.actions.rules import evaluate_event, evaluate_link
from app.db.models import Task, TaskAction, TaskEvent, TaskThreadLink

log = logging.getLogger(__name__)


def fire_rules_for_event(
    db: Session, *, user_id: str, task: Task, event: TaskEvent,
    thread_id: str, gmail_thread_id: str, publish, rules=None,
) -> list[TaskAction]:
    """Evaluate + insert TaskActions for one just-applied TaskEvent, then
    dispatch each inserted action (mode='propose' -> the SSE nudge only;
    mode='auto' -> also enqueue execute_action).

    Callers must only invoke this for a genuinely APPLIED event (matching
    evaluate_event's own contract) — every site that commits an applied
    TaskEvent: workers/task_engine_tasks.py's process_task_updates/
    extract_for_thread/_run_tracker_backfill's extraction phase, and
    api/tasks.py's approve_event/edit_entity_state. refold_entity's own
    recomputation is deliberately NOT such a site — it never calls
    repo.apply_event (it directly reassigns entity.state from a fold over
    already-applied events), so it never reaches these hooks; see
    task_engine/repo.py's module docstring for that distinction.

    `task.kind` must be "tracker" — bucket-kind tasks structurally never get
    rules (spec §2's task_id FK is enforced tracker-only at the API layer),
    but this is defense-in-depth against a stray call: returns [] with no
    query at all for a non-tracker task.

    `rules` (optional): when provided, skip the list_rules() query and use
    these pre-loaded rules instead. Useful when firing multiple rules within
    a batch loop to avoid N+1 queries per task.
    """
    if task.kind != "tracker":
        return []
    if rules is None:
        rules = actions_repo.list_rules(db, task_id=task.id)
    if not rules:
        return []
    intents = evaluate_event(
        event, rules=rules, thread_id=thread_id, gmail_thread_id=gmail_thread_id,
    )
    return _insert_and_dispatch(db, user_id=user_id, task=task, rules=rules, intents=intents, publish=publish)


def fire_rules_for_link(
    db: Session, *, user_id: str, task: Task, link: TaskThreadLink,
    thread_id: str, gmail_thread_id: str, publish, rules=None,
) -> list[TaskAction]:
    """Same contract as fire_rules_for_event, for a freshly-attached
    TaskThreadLink (thread_linked rules). Callers MUST only call this for a
    link whose upsert_link() call reported `newly_attached=True` — a
    confidence/origin refresh of an already-attached link, or a detach
    (state='detached'), must never refire (see task_engine.repo.upsert_link's
    LinkUpsert contract for exactly what newly_attached means).

    `rules` (optional): when provided, skip the list_rules() query and use
    these pre-loaded rules instead. Useful when firing multiple rules within
    a batch loop to avoid N+1 queries per task.
    """
    if task.kind != "tracker":
        return []
    if rules is None:
        rules = actions_repo.list_rules(db, task_id=task.id)
    if not rules:
        return []
    intents = evaluate_link(
        link, rules=rules, thread_id=thread_id, gmail_thread_id=gmail_thread_id,
    )
    return _insert_and_dispatch(db, user_id=user_id, task=task, rules=rules, intents=intents, publish=publish)


def _insert_and_dispatch(
    db: Session, *, user_id: str, task: Task, rules: list, intents: list, publish,
) -> list[TaskAction]:
    """Shared tail of both fire_rules_for_*: insert each evaluated intent
    (None on a replay/idempotency conflict -> skip, never an error), commit
    once, then per INSERTED action publish the `action_updated {task_id}`
    pure nudge (events-carry-ids convention — the client refetches reviews/
    activity off it, mirroring job_updated) and, for a mode='auto' rule,
    enqueue execute_action.

    draft_reply is asserted to never reach the mode='auto' branch here — the
    rule layer (the rules CRUD API) rejects writing such a rule at all, so
    this assert is the SECOND line of defense; execute_action itself carries
    a THIRD (belt + suspenders x2, per spec §6 invariant 2 — a safety
    invariant this important gets more than one independent guard).
    """
    if not intents:
        return []

    rules_by_id = {r.id: r for r in rules}
    inserted: list[TaskAction] = []
    for intent in intents:
        action = actions_repo.insert_intent(db, task_id=task.id, intent=intent)
        if action is None:
            continue  # replay / idempotency conflict -- already fired for this evidence
        inserted.append(action)
    db.commit()

    for action in inserted:
        publish(user_id, "action_updated", {"task_id": task.id})
        rule = rules_by_id.get(action.rule_id)
        if rule is not None and rule.mode == "auto":
            # Explicit guard (not assert): draft_reply rules must never be mode='auto'.
            # First line: rules CRUD API rejects writing such a rule. Second line:
            # this dispatch engine. Third line: execute_action itself (spec §6 invariant 2).
            if action.action_type == "draft_reply":
                raise RuntimeError("draft_reply actions can never auto-execute (spec 006 invariant #2)")
            # Late import: workers/action_tasks.py imports app.workers.celery_app
            # (which itself imports and registers every task module via its
            # `include=[...]` list) -- keeping this import inside the function
            # body avoids this module needing to know anything about celery's
            # app-construction/import-timing at its own top level, mirroring
            # the late-import discipline workers/task_engine_tasks.py already
            # uses for its own cross-module calls.
            from app.workers.action_tasks import execute_action

            execute_action.apply_async(args=[user_id, action.id])

    return inserted
