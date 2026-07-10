"""Jobs HTTP API (Phase 4.5 Task 3, spec 005): the persisted, pollable
surface behind the header chip + slide-over panel — replaces the old
fire-and-forget `task_draft_ready` popup that could strand a client forever
after an SSE blip.

Mirrors app/api/tasks.py's shape (ownership 404s via `jobs_repo.get_owned_job`
— no 403-vs-404 split; pydantic request bodies; a `_serialize_job` helper;
the caller commits, not the repo) and reuses `app.api.tasks._create_task_
from_fields`/`_publish_task_updated` for the confirm route's task-creation
step, so the kind-aware 422 rules and criteria grammar can never drift from
`POST /api/tasks`'s own direct-creation path.

Two job kinds exist per the jobs table (`kind`: 'creation' | 'delete_
retriage'), but only 'creation' has an HTTP surface today — a
'delete_retriage' job is created and driven entirely worker-side (bucket
delete re-triage) and is only ever read back via `GET /api/jobs*`, never
posted to directly.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.tasks import (
    _ExampleIn, _create_task_from_fields, _publish_task_updated, _serialize_task_detail,
)
from app.db.models import User
from app.db.session import get_db
from app.deps import get_current_user
from app.task_engine import jobs_repo
from app.workers import task_engine_tasks
from app.workers import tasks

router = APIRouter(prefix="/api", tags=["jobs"])
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serializer
# ---------------------------------------------------------------------------


def _serialize_job(job) -> dict:
    """Every `Job` column except `user_id` (spec §1.1) — dropped by
    construction (an explicit field list) rather than an allow-list that
    could silently start leaking a new sensitive column later."""
    return {
        "id": job.id,
        "kind": job.kind,
        "task_kind": job.task_kind,
        "stage": job.stage,
        "needs_user": job.needs_user,
        "payload": job.payload,
        "task_id": job.task_id,
        "goal": job.goal,
        "scanned": job.scanned,
        "matched": job.matched,
        "total": job.total,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "dismissed_at": job.dismissed_at,
    }


def _require_owned_job(db: Session, *, user_id: str, job_id: str):
    job = jobs_repo.get_owned_job(db, user_id=user_id, job_id=job_id)
    if job is None:
        raise HTTPException(404, "not found")
    return job


# ---------------------------------------------------------------------------
# Create + poll
# ---------------------------------------------------------------------------


class _CreateJobBody(BaseModel):
    goal: str = Field(min_length=1)
    task_kind: Literal["tracker", "bucket"]


@router.post("/jobs", status_code=202)
def create_job(body: _CreateJobBody, user: User = Depends(get_current_user),
              db: Session = Depends(get_db)) -> dict:
    """Start the goal -> draft flow: a `creation` job row (stage='proposing')
    plus the propose worker enqueue. Replaces the old `POST /tasks/draft`."""
    job = jobs_repo.create_job(
        db, user_id=user.id, kind="creation", task_kind=body.task_kind, goal=body.goal,
    )
    db.commit()
    task_engine_tasks.propose_task_draft.apply_async(
        args=[user.id, job.id, body.goal], countdown=0,
    )
    return {"job": _serialize_job(job)}


@router.get("/jobs")
def list_jobs(
    active: int = Query(default=1),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> dict:
    """The panel's always-works poll path. `active=1` (default) is the
    panel's normal view (non-dismissed, non-terminal-or-recently-terminal —
    see `jobs_repo.list_jobs`'s active-window semantics); `active=0` returns
    every non-dismissed job regardless of age."""
    jobs = jobs_repo.list_jobs(db, user_id=user.id, active_only=bool(active))
    return {"jobs": [_serialize_job(j) for j in jobs]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(get_current_user),
           db: Session = Depends(get_db)) -> dict:
    job = _require_owned_job(db, user_id=user.id, job_id=job_id)
    return {"job": _serialize_job(job)}


# ---------------------------------------------------------------------------
# Confirm: draft_ready -> task creation -> backfilling
# ---------------------------------------------------------------------------


class _ConfirmJobBody(BaseModel):
    """Same fields as `tasks._CreateTaskBody` minus `kind` (the job's own
    `task_kind`, fixed at `POST /api/jobs` time, governs) and minus `goal`
    (already stored on the job row from that same request — the review step
    has no reason to make the user retype it)."""
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    state_schema: dict | None = None
    keyword_probes: list[str] = Field(default_factory=list)
    confirmed_positives: list[_ExampleIn] = Field(default_factory=list)
    confirmed_negatives: list[_ExampleIn] = Field(default_factory=list)


@router.post("/jobs/{job_id}/confirm")
def confirm_job(job_id: str, body: _ConfirmJobBody, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)) -> dict:
    """Only legal from `draft_ready` (409 otherwise). Creates the task
    through the exact same kind-aware internals `POST /api/tasks` uses
    (`_create_task_from_fields`, shared — see that function's docstring),
    then folds the job's own `task_id`/`stage='backfilling'` update into the
    SAME commit as the task insert, so the two rows never diverge (a task
    created but its job stuck in `draft_ready`, or vice versa). Enqueues
    `backfill_task` with `job_id` set (unlike the direct-create path's plain
    backfill, this run also writes progress into the job row — see
    `workers/task_engine_tasks.py`), then publishes both `task_updated`
    (parity with direct creation) and `job_updated`.
    """
    job = _require_owned_job(db, user_id=user.id, job_id=job_id)
    # Spec invariant: a dismissed draft_ready job simply never confirms
    if job.dismissed_at is not None or job.stage != "draft_ready":
        raise HTTPException(409, "job is not awaiting review")

    task = _create_task_from_fields(
        db, user_id=user.id, name=body.name, goal=job.goal, description=body.description,
        kind=job.task_kind, state_schema=body.state_schema,
        confirmed_positives=body.confirmed_positives, confirmed_negatives=body.confirmed_negatives,
    )
    job.task_id = task.id
    jobs_repo.update_stage(db, job=job, stage="backfilling")
    db.commit()

    task_engine_tasks.backfill_task.apply_async(
        args=[user.id, task.id, body.keyword_probes], kwargs={"job_id": job.id}, countdown=0,
    )
    _publish_task_updated(db, user_id=user.id, task=task)
    tasks._publish(user.id, "job_updated", {"job_id": job.id})

    return {"task": _serialize_task_detail(db, task), "job": _serialize_job(job)}


# ---------------------------------------------------------------------------
# Dismiss
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/dismiss", status_code=204)
def dismiss_job(job_id: str, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)) -> None:
    """Idempotent — `jobs_repo.dismiss` no-ops on an already-dismissed job,
    so a second call still returns 204 rather than erroring."""
    job = _require_owned_job(db, user_id=user.id, job_id=job_id)
    jobs_repo.dismiss(db, job=job)
    db.commit()
