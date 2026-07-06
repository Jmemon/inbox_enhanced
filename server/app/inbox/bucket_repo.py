"""Bucket CRUD helpers shared by api endpoints + workers.

Caller owns the transaction (Session). This module never commits. Endpoints
in api/buckets.py wrap each call in a normal request lifecycle; workers wrap
in their task-level commit.

Default-bucket protection (no rename/delete on user_id=None) is enforced at
the API layer (it needs to return 403, not silently fail), not here. This
keeps the repo callable from internal code that should be allowed to
mutate defaults if necessary.
"""

import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.models import Bucket
from app.task_engine.criteria import formulate_criteria  # noqa: F401


def list_active(db: Session, *, user_id: str) -> list[Bucket]:
    """Defaults (user_id IS NULL) + this user's custom buckets where
    is_deleted = False. Sorted by name for stable api output."""
    stmt = (
        select(Bucket)
        .where(Bucket.is_deleted.is_(False))
        .where((Bucket.user_id.is_(None)) | (Bucket.user_id == user_id))
        .order_by(Bucket.name.asc())
    )
    return list(db.execute(stmt).scalars().all())


def get_by_id(db: Session, bucket_id: str) -> Bucket | None:
    """Bare lookup — does not check ownership or is_deleted. Endpoints layer
    on the policy: PATCH/DELETE check `bucket.user_id == request_user_id`
    and return 403 / 404 accordingly."""
    return db.get(Bucket, bucket_id)


def create_custom(db: Session, *, user_id: str, name: str, criteria: str) -> Bucket:
    """Insert a user-owned bucket. Returns the new row (caller commits)."""
    row = Bucket(
        id=uuid.uuid4().hex,
        user_id=user_id,
        name=name,
        criteria=criteria,
        is_deleted=False,
    )
    db.add(row)
    db.flush()
    return row


def rename(db: Session, bucket: Bucket, name: str) -> Bucket:
    bucket.name = name
    return bucket


def soft_delete(db: Session, bucket: Bucket) -> Bucket:
    bucket.is_deleted = True
    return bucket
