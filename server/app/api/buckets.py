"""Bucket HTTP API.

SHIM -- Phase 4 back-compat for one release. Backed by tasks(kind='bucket').
Delete in Phase 5.

Four routes: GET / POST / PATCH / DELETE. The draft-preview routes (POST
/buckets/draft/preview, GET /buckets/draft/preview/{id}) and their
preview_cache/draft_preview_bucket machinery are gone -- superseded by the
task engine's own goal->draft flow (POST /api/tasks/draft). A stale
pre-deploy client tab that still calls either deleted route gets a plain
404; NewBucketModal's existing `gone` polling handler already treats any
404 that way.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.db.models import User, Task
from app.db.session import get_db
from app.deps import get_current_user
from app.inbox import bucket_repo
from app.workers import task_engine_tasks


router = APIRouter(prefix="/api", tags=["buckets"])
log = logging.getLogger(__name__)


def _serialize(b: Task) -> dict:
    return {"id": b.id, "name": b.name, "criteria": b.criteria, "is_default": b.user_id is None}


class _ExampleIn(BaseModel):
    sender: str = ""
    subject: str = ""
    snippet: str = ""
    rationale: str = ""


class _CreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    confirmed_positives: list[_ExampleIn] = Field(default_factory=list)
    confirmed_negatives: list[_ExampleIn] = Field(default_factory=list)


class _PatchBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)


def _load_owned_or_403(db: Session, user_id: str, bucket_id: str) -> Task:
    """Load a bucket the user can mutate (PATCH/DELETE). Default → 403,
    other-user → 403, missing/soft-deleted → 404."""
    b = bucket_repo.get_by_id(db, bucket_id)
    if b is None:
        raise HTTPException(404, "not found")
    if b.user_id is None:
        raise HTTPException(403, "cannot modify default bucket")
    if b.user_id != user_id:
        raise HTTPException(403, "not your bucket")
    if b.is_deleted:
        raise HTTPException(404, "not found")
    return b


@router.get("/buckets")
def list_buckets(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    rows = bucket_repo.list_active(db, user_id=user.id)
    return {"buckets": [_serialize(b) for b in rows]}


@router.post("/buckets", status_code=201)
def create_bucket(body: _CreateBody, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)) -> dict:
    criteria = bucket_repo.formulate_criteria(
        description=body.description,
        confirmed_positives=[e.model_dump() for e in body.confirmed_positives],
        confirmed_negatives=[e.model_dump() for e in body.confirmed_negatives],
    )
    row = bucket_repo.create_custom(db, user_id=user.id, name=body.name, criteria=criteria)
    db.commit()
    # Phase 4 Task 2: a bucket is now tasks(kind='bucket'), so a fresh custom
    # bucket is backfilled the same way a fresh tracker is -- backfill_task's
    # kind='bucket' branch reclassifies the user's stored inbox history
    # against the new (full) bucket set so this bucket can pick up matching
    # threads that are already synced. keyword_probes=[] -- a bucket has no
    # LLM-proposed search terms the way a tracker's wizard does, so the
    # FTS-probe prefilter degrades to backfill_task's recency-window fallback
    # over the user's whole history. Async — the user gets 201 immediately,
    # the inbox view updates via the threads_updated SSE event when the
    # worker finishes.
    task_engine_tasks.backfill_task.apply_async(
        args=[user.id, row.id, []], countdown=0,
    )
    return _serialize(row)


@router.patch("/buckets/{bucket_id}")
def patch_bucket(bucket_id: str, body: _PatchBody,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    b = _load_owned_or_403(db, user.id, bucket_id)
    bucket_repo.rename(db, b, body.name); db.commit()
    return _serialize(b)


@router.delete("/buckets/{bucket_id}", status_code=204)
def delete_bucket(bucket_id: str, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)) -> None:
    # Don't 404 on already-deleted — DELETE is idempotent.
    b = bucket_repo.get_by_id(db, bucket_id)
    if b is None: raise HTTPException(404, "not found")
    if b.user_id is None: raise HTTPException(403, "cannot delete default bucket")
    if b.user_id != user.id: raise HTTPException(403, "not your bucket")
    if b.is_deleted: return
    bucket_repo.soft_delete(db, b); db.commit()
