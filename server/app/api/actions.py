"""Phase 5 (actions, spec 006) Task 4 HTTP surface: rules CRUD
(`task_action_rules`) and the action lifecycle (`task_actions`) — approve/
reject/undo. One home for every route this task adds; the typed reviews/
activity feed merge lives in `api/tasks.py` instead (it extends two routes
that already live there).

Mirrors app/api/tasks.py's shape throughout: owner-scoped via a Task join
(no 403-vs-404 split — a wrong-user id and a nonexistent id look identical
on the wire), pydantic request bodies, small `_serialize_*` helpers, the
caller commits (not the repo). `_require_owned_task` is intentionally a
LOCAL copy of api/tasks.py's own helper (task_repo.get_owned_task directly)
rather than an import — api/tasks.py never imports this module, so importing
FROM it would be safe, but keeping this one line local avoids any risk of
that direction ever flipping into a cycle as both modules grow. Rule-mutation
routes DO reuse `app.api.tasks._publish_task_updated` (import-safe: tasks.py
has no dependency on this module) so every task-affecting mutation — rules
included — funnels through the exact same `task_updated` publish helper
(mirrors app/api/jobs.py's own reuse of it).

Action routes publish `action_updated {task_id}` directly (the pure-nudge
convention app.actions.engine already established) rather than through
_publish_task_updated -- approving/rejecting/undoing one action doesn't
change task.version or pending_count in a way `task_updated` describes.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sqlalchemy.orm import Session

from app.actions import repo as actions_repo
from app.api.tasks import _publish_task_updated
from app.db.models import User
from app.db.session import get_db
from app.deps import get_current_user
from app.gmail import client as gmail_client
from app.task_engine import repo as task_repo
from app.task_engine import schema as schema_mod
from app.workers import action_tasks
from app.workers import tasks


router = APIRouter(prefix="/api", tags=["actions"])
log = logging.getLogger(__name__)


def _require_owned_task(db: Session, *, user_id: str, task_id: str):
    task = task_repo.get_owned_task(db, user_id=user_id, task_id=task_id)
    if task is None:
        raise HTTPException(404, "not found")
    return task


# ---------------------------------------------------------------------------
# Rule validation vocab (spec §2 + §6 invariant 2, §6 invariant 9)
# ---------------------------------------------------------------------------


_TRIGGERS = {"entity_entered_stage", "thread_linked"}
_ACTION_TYPES = {"archive_thread", "label_thread", "draft_reply"}
_MODES = {"propose", "auto"}
# Gmail's own reserved label names (case-insensitive) -- defense in depth on
# top of gmail_client.label_thread's own user-labels-only matching (T2): a
# rule must never be able to write a label param that later resolves to a
# system label at execute time.
_GMAIL_SYSTEM_LABELS = {
    "inbox", "spam", "trash", "unread", "starred", "important", "sent", "draft", "chat",
}


def _validate_rule_fields(
    task, *, trigger: str, trigger_params: dict | None, action_type: str,
    action_params: dict | None, mode: str,
) -> None:
    """Full-shape validation shared by create and patch (patch runs this
    against the MERGED post-patch fields rather than diffing which changed --
    simpler, and a no-op patch that resubmits already-valid fields is
    harmless). Raises HTTPException(422, ...) on the first violation found."""
    if trigger not in _TRIGGERS:
        raise HTTPException(422, "trigger must be 'entity_entered_stage' or 'thread_linked'")

    if trigger == "entity_entered_stage":
        stage = (trigger_params or {}).get("stage")
        if not stage:
            raise HTTPException(422, "trigger_params.stage is required for entity_entered_stage")
        if task.state_schema is None:
            raise HTTPException(422, "task has no valid schema for stage rules")
        try:
            schema = schema_mod.validate_schema(task.state_schema)
        except ValueError:
            raise HTTPException(422, "task has no valid schema for stage rules")
        if stage not in schema.all_stages():
            raise HTTPException(422, f"'{stage}' is not a valid stage")
    else:  # thread_linked takes no params
        if trigger_params:
            raise HTTPException(422, "thread_linked takes no trigger_params")

    if action_type not in _ACTION_TYPES:
        raise HTTPException(422, "action_type must be one of archive_thread, label_thread, draft_reply")

    if action_type == "label_thread":
        label = ((action_params or {}).get("label") or "").strip()
        if not label:
            raise HTTPException(422, "label_thread requires a non-empty action_params.label")
        if label.lower() in _GMAIL_SYSTEM_LABELS:
            raise HTTPException(422, "label name conflicts with a Gmail system label")
    elif action_type == "draft_reply":
        instructions = ((action_params or {}).get("instructions") or "").strip()
        if not instructions:
            raise HTTPException(422, "draft_reply requires non-empty action_params.instructions")

    if mode not in _MODES:
        raise HTTPException(422, "mode must be 'propose' or 'auto'")
    if action_type == "draft_reply" and mode == "auto":
        # Spec §6 invariant 2, first line of defense (engine.py's dispatch
        # assert + execute_action's Celery-entry refusal are the other two).
        raise HTTPException(422, "draft_reply cannot auto-run")


def _serialize_rule(rule) -> dict:
    """All columns, per T4 brief."""
    return {
        "id": rule.id,
        "task_id": rule.task_id,
        "trigger": rule.trigger,
        "trigger_params": rule.trigger_params,
        "action_type": rule.action_type,
        "action_params": rule.action_params,
        "mode": rule.mode,
        "is_deleted": rule.is_deleted,
        "created_at": rule.created_at,
    }


class _RuleCreateBody(BaseModel):
    trigger: str
    trigger_params: dict | None = None
    action_type: str
    action_params: dict | None = None
    mode: str


class _RulePatchBody(BaseModel):
    trigger: str | None = None
    trigger_params: dict | None = None
    action_type: str | None = None
    action_params: dict | None = None
    mode: str | None = None


@router.post("/tasks/{task_id}/rules", status_code=201)
def create_rule(task_id: str, body: _RuleCreateBody, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    if task.kind != "tracker":
        # Spec §6 invariant 9: buckets have no events/links to trigger off of
        # in the first place -- this guard makes that explicit rather than
        # relying on the FK alone.
        raise HTTPException(422, "rules are tracker-only")

    _validate_rule_fields(
        task, trigger=body.trigger, trigger_params=body.trigger_params,
        action_type=body.action_type, action_params=body.action_params, mode=body.mode,
    )
    rule = actions_repo.create_rule(
        db, task_id=task.id, trigger=body.trigger, trigger_params=body.trigger_params,
        action_type=body.action_type, action_params=body.action_params, mode=body.mode,
    )
    # Rule mutations bump task.version + publish task_updated -- the client's
    # rules section on the task page converges off the same SSE signal every
    # other task mutation already uses (see api/tasks.py's _PatchTaskBody
    # handler for the identical rationale).
    task_repo.bump_version(db, task=task)
    db.commit()
    _publish_task_updated(db, user_id=user.id, task=task)
    return _serialize_rule(rule)


@router.get("/tasks/{task_id}/rules")
def list_rules(task_id: str, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    rules = actions_repo.list_rules(db, task_id=task.id)
    return {"rules": [_serialize_rule(r) for r in rules]}


def _require_owned_rule(db: Session, *, user_id: str, task, rule_id: str):
    """Owner-scoped rule lookup that ALSO confirms the rule belongs to the
    task named in the path -- get_owned_rule alone scopes by user (any of
    their tasks), not by this specific task_id, so a rule_id that's valid
    but under a DIFFERENT task of this same user must still 404 here (wrong
    path, not just wrong owner)."""
    rule = actions_repo.get_owned_rule(db, user_id=user_id, rule_id=rule_id)
    if rule is None or rule.task_id != task.id:
        raise HTTPException(404, "not found")
    return rule


@router.patch("/tasks/{task_id}/rules/{rule_id}")
def patch_rule(task_id: str, rule_id: str, body: _RulePatchBody,
               user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    rule = _require_owned_rule(db, user_id=user.id, task=task, rule_id=rule_id)

    # Only fields the client actually sent are considered "changed" --
    # exclude_unset (not a plain None-check) so a client omitting
    # trigger_params/action_params leaves the existing value alone rather
    # than reading as "clear it to null".
    provided = body.model_dump(exclude_unset=True)
    trigger = provided.get("trigger", rule.trigger)
    trigger_params = provided.get("trigger_params", rule.trigger_params)
    action_type = provided.get("action_type", rule.action_type)
    action_params = provided.get("action_params", rule.action_params)
    mode = provided.get("mode", rule.mode)

    # Validate the MERGED post-patch shape (not a field-by-field diff) --
    # every rule, patched or not, must satisfy the full invariant set.
    _validate_rule_fields(
        task, trigger=trigger, trigger_params=trigger_params,
        action_type=action_type, action_params=action_params, mode=mode,
    )
    actions_repo.update_rule(
        db, rule=rule, trigger=trigger, trigger_params=trigger_params,
        action_type=action_type, action_params=action_params, mode=mode,
    )
    task_repo.bump_version(db, task=task)
    db.commit()
    _publish_task_updated(db, user_id=user.id, task=task)
    return _serialize_rule(rule)


@router.delete("/tasks/{task_id}/rules/{rule_id}", status_code=204)
def delete_rule(task_id: str, rule_id: str, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)) -> None:
    """Soft delete, idempotent like tasks' own DELETE: a second call against
    the same (owned, same-task) rule_id is a silent no-op -- get_owned_rule
    doesn't filter its own is_deleted (see its docstring), so a single lookup
    here distinguishes "already deleted" (204 no-op, no bump/publish) from
    "not yours / wrong task" (404)."""
    task = _require_owned_task(db, user_id=user.id, task_id=task_id)
    rule = _require_owned_rule(db, user_id=user.id, task=task, rule_id=rule_id)
    if rule.is_deleted:
        return

    actions_repo.soft_delete_rule(db, rule=rule)
    task_repo.bump_version(db, task=task)
    db.commit()
    _publish_task_updated(db, user_id=user.id, task=task)


# ---------------------------------------------------------------------------
# Actions: approve / reject / undo
# ---------------------------------------------------------------------------


def _serialize_action(action) -> dict:
    return {
        "id": action.id,
        "task_id": action.task_id,
        "rule_id": action.rule_id,
        "source_event_id": action.source_event_id,
        "source_link_id": action.source_link_id,
        "thread_id": action.thread_id,
        "gmail_thread_id": action.gmail_thread_id,
        "action_type": action.action_type,
        "action_params": action.action_params,
        "status": action.status,
        "result": action.result,
        "error": action.error,
        "created_at": action.created_at,
        "executed_at": action.executed_at,
    }


def _require_owned_action(db: Session, *, user_id: str, action_id: str):
    action = actions_repo.get_owned_action(db, user_id=user_id, action_id=action_id)
    if action is None:
        raise HTTPException(404, "not found")
    return action


@router.post("/actions/{action_id}/approve")
def approve_action(action_id: str, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)) -> dict:
    """Synchronous execute -- shares `execute_action_inner` (app.workers.
    action_tasks) with the mode='auto' Celery path, one dispatch function for
    both entry points. `execute_action_inner` itself re-checks the action's
    evidence hasn't been reverted/detached since it was proposed; when that
    check fails it flips the row to 'rejected' and commits -- we surface that
    as 409 "action source no longer valid" rather than 200, since nothing
    the user asked for happened. Both other outcomes (executed, or failed on
    a MissingScopesError) return 200 with the action body: the HTTP request
    itself succeeded either way, only the ACTION's own status tells the
    client whether the Gmail write actually landed.

    Any exception OTHER than the ones execute_action_inner already handles
    propagates out to a 500 -- but only after a guarded rollback + a
    best-effort 'failed' status write, mirroring execute_action's own
    Celery-wrapper failure discipline (see that function's docstring) so a
    synchronous approve crash leaves the same audit trail an async auto-fire
    crash would. Unlike the Celery wrapper's `_record_action_failure` (which
    must open a brand-new session because a worker's own `db` has no
    request-scoped lifecycle to fall back on), this reuses the SAME `db` --
    a rolled-back Session is immediately usable again within the same
    FastAPI request, and it's already bound to the right engine via
    get_db's dependency override, which a second, independently-configured
    SessionLocal is not guaranteed to be in tests.
    """
    action = _require_owned_action(db, user_id=user.id, action_id=action_id)
    if action.status != "proposed":
        raise HTTPException(409, "action is not pending")

    try:
        action_tasks.execute_action_inner(db, user=user, action=action, publish=tasks._publish)
    except Exception as exc:
        log.exception("approve_action: action=%s failed", action_id)
        try:
            db.rollback()
        except Exception:
            log.exception("approve_action: rollback failed; original error takes precedence")
        else:
            try:
                actions_repo.set_status(db, action=action, status="failed", error=str(exc))
                db.commit()
                tasks._publish(user.id, "action_updated", {"task_id": action.task_id})
            except Exception:
                log.exception("approve_action: failed to record action failure for action=%s", action_id)
        raise

    if action.status == "rejected":
        raise HTTPException(409, "action source no longer valid")
    return _serialize_action(action)


@router.post("/actions/{action_id}/reject")
def reject_action(action_id: str, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)) -> dict:
    action = _require_owned_action(db, user_id=user.id, action_id=action_id)
    if action.status != "proposed":
        raise HTTPException(409, "action is not pending")

    actions_repo.set_status(db, action=action, status="rejected")
    db.commit()
    tasks._publish(user.id, "action_updated", {"task_id": action.task_id})
    return _serialize_action(action)


_UNDOABLE_ACTION_TYPES = {"archive_thread", "label_thread"}


@router.post("/actions/{action_id}/undo")
def undo_action(action_id: str, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)) -> dict:
    """Only 'executed' + a reversible action_type can be undone (draft_reply
    has no undo -- delete the draft in Gmail if unwanted, per spec §3).
    Replays the exact inverse of `result` via gmail_client.modify_thread_labels:
    archive_thread's undo re-adds `result["removed_label_ids"]` (always
    ["INBOX"]); label_thread's undo removes `result["added_label_ids"]`
    (the one label id it added).

    A MissingScopesError here (scopes revoked since the original execute --
    rare) is reported the SAME way as the wrong-status precondition: 409,
    detail carrying "needs permission: ...". One consistent shape for every
    reason this route can't complete, rather than a 200-with-error-body
    special case for this one failure mode.
    """
    action = _require_owned_action(db, user_id=user.id, action_id=action_id)
    if action.status != "executed" or action.action_type not in _UNDOABLE_ACTION_TYPES:
        raise HTTPException(409, "action cannot be undone")

    result = action.result or {}
    try:
        if action.action_type == "archive_thread":
            gmail_client.modify_thread_labels(
                db, user, action.gmail_thread_id, add=result.get("removed_label_ids") or [],
            )
        else:  # label_thread
            gmail_client.modify_thread_labels(
                db, user, action.gmail_thread_id, remove=result.get("added_label_ids") or [],
            )
    except gmail_client.MissingScopesError as exc:
        raise HTTPException(409, str(exc))

    actions_repo.set_status(db, action=action, status="undone")
    db.commit()
    tasks._publish(user.id, "action_updated", {"task_id": action.task_id})
    return _serialize_action(action)
