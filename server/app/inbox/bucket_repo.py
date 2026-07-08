"""Bucket CRUD helpers shared by api endpoints + workers.

Task-backed since Phase 4; buckets are tasks(kind='bucket'). This module
keeps its five original signatures (returning/accepting Task rows, not a
dedicated Bucket model) so api/buckets.py, workers/tasks.py, and
workers/gmail_sync.py — which duck-type .id/.name/.criteria/.user_id/
.is_deleted — kept working unchanged across the migration.

Caller owns the transaction (Session). This module never commits. Endpoints
in api/buckets.py wrap each call in a normal request lifecycle; workers wrap
in their task-level commit.

Default-bucket protection (no rename/delete on user_id=None) is enforced at
the API layer (it needs to return 403, not silently fail), not here. This
keeps the repo callable from internal code that should be allowed to
mutate defaults if necessary.
"""

from sqlalchemy.orm import Session
from app.db.models import Task
from app.task_engine import repo as task_repo
from app.task_engine.criteria import formulate_criteria  # noqa: F401


def list_active(db: Session, *, user_id: str) -> list[Task]:
    """Defaults (user_id IS NULL) + this user's custom buckets where
    is_deleted = False. Sorted by name for stable api output."""
    return task_repo.list_active_buckets(db, user_id=user_id)


def get_by_id(db: Session, bucket_id: str) -> Task | None:
    """Bare lookup — does not check ownership or is_deleted. Endpoints layer
    on the policy: PATCH/DELETE check `bucket.user_id == request_user_id`
    and return 403 / 404 accordingly. Returns None for a non-bucket-kind
    task id (e.g. a tracker) — bucket_id-shaped lookups must never resolve
    to a tracker row."""
    row = db.get(Task, bucket_id)
    if row is None or row.kind != "bucket":
        return None
    return row


def create_custom(db: Session, *, user_id: str, name: str, criteria: str) -> Task:
    """Insert a user-owned bucket. Returns the new row (caller commits)."""
    return task_repo.create_task(
        db, user_id=user_id, name=name, goal="", criteria=criteria,
        state_schema=None, kind="bucket",
    )


def rename(db: Session, bucket: Task, name: str) -> Task:
    bucket.name = name
    return bucket


def soft_delete(db: Session, bucket: Task) -> Task:
    bucket.is_deleted = True
    return bucket
