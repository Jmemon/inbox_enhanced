"""Jobs CRUD: the persisted, pollable progress rows behind the jobs surface
(Phase 4.5, spec 005) — the creation wizard's goal -> draft -> backfill flow
and bucket-delete re-triage.

Caller owns the transaction (Session) — this module never commits, matching
task_engine.repo and app.inbox.{bucket_repo,inbox_repo}. Write-path helpers
that INSERT a new row call db.flush() afterward so the generated id is
visible to the caller before commit; every other write helper mutates an
already-loaded Job in place and does not flush. Job.updated_at has no
onupdate= (see db/models.py) — every write helper here is responsible for
touching it explicitly.

Ids are uuid.uuid4().hex; timestamps are datetime.now(timezone.utc).
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job

# list_jobs' active window: a terminal-stage job stays visible for this long
# after its last update before aging out of the default (active_only) query.
_ACTIVE_WINDOW = timedelta(days=7)

_TERMINAL_STAGES = ("done", "failed")

# create_job derives the initial stage from kind — the two stage machines'
# entry points (spec §1.2).
_INITIAL_STAGE_BY_KIND = {
    "creation": "proposing",
    "delete_retriage": "running",
}


def create_job(
    db: Session, *, user_id: str, kind: str, task_kind: str | None = None, goal: str = "",
) -> Job:
    """Insert a new job row. Initial stage derives from kind: 'proposing' for
    'creation', 'running' for 'delete_retriage'. Raises ValueError for any
    other kind — there is no third stage machine to default to."""
    if kind not in _INITIAL_STAGE_BY_KIND:
        raise ValueError(f"unknown job kind: {kind!r}")

    now = datetime.now(timezone.utc)
    row = Job(
        id=uuid.uuid4().hex,
        user_id=user_id,
        kind=kind,
        task_kind=task_kind,
        stage=_INITIAL_STAGE_BY_KIND[kind],
        needs_user=False,
        goal=goal,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def get_owned_job(db: Session, *, user_id: str, job_id: str) -> Job | None:
    """Fetch one job scoped to its owner. Returns None for wrong-user or
    nonexistent alike (no enumeration split), mirroring
    task_engine.repo.get_owned_task."""
    return db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == user_id)
    ).scalar_one_or_none()


def list_jobs(db: Session, *, user_id: str, active_only: bool = True) -> list[Job]:
    """This user's jobs, newest first (created_at desc).

    active_only=True (the panel's default) applies the active window:
    dismissed_at IS NULL AND (stage is non-terminal OR updated_at is within
    the last 7 days). The cutoff is computed here in Python as a tz-aware
    datetime rather than via SQL now() — the migration/repo test suite runs
    this against SQLite, which has no equivalent of Postgres's now(), and a
    Python-computed cutoff behaves identically on both engines.
    """
    stmt = select(Job).where(Job.user_id == user_id)
    if active_only:
        cutoff = datetime.now(timezone.utc) - _ACTIVE_WINDOW
        stmt = stmt.where(
            Job.dismissed_at.is_(None),
            (Job.stage.notin_(_TERMINAL_STAGES)) | (Job.updated_at >= cutoff),
        )
    stmt = stmt.order_by(Job.created_at.desc())
    return list(db.execute(stmt).scalars().all())


def update_stage(db: Session, *, job: Job, stage: str) -> None:
    """Transition job.stage. needs_user is denormalized true only for
    stage='draft_ready' (the header chip's blue-dot query) and cleared on
    every other transition, including into failed/dismissed."""
    job.stage = stage
    job.needs_user = (stage == "draft_ready")
    job.updated_at = datetime.now(timezone.utc)


def update_progress(db: Session, *, job: Job, scanned: int, matched: int, total: int) -> None:
    """Overwrite the batch progress counters — called per-batch by
    backfill_task and the delete_retriage worker."""
    job.scanned = scanned
    job.matched = matched
    job.total = total
    job.updated_at = datetime.now(timezone.utc)


def set_payload(db: Session, *, job: Job, payload: dict) -> None:
    """Write the proposed draft (creation jobs only) — read back by the
    review step when the job reaches draft_ready."""
    job.payload = payload
    job.updated_at = datetime.now(timezone.utc)


def mark_failed(db: Session, *, job: Job, error: str) -> None:
    """Terminal failure from any stage. Clears needs_user — a failed job is
    never rendered with the blue dot, only its error text."""
    job.stage = "failed"
    job.error = error
    job.needs_user = False
    job.updated_at = datetime.now(timezone.utc)


def dismiss(db: Session, *, job: Job) -> None:
    """User-initiated dismissal — idempotent: a second call on an
    already-dismissed job is a no-op (dismissed_at is never overwritten once
    set, and updated_at is left untouched)."""
    if job.dismissed_at is not None:
        return
    job.dismissed_at = datetime.now(timezone.utc)
