"""Bucket HTTP API. Four routes: GET / POST / PATCH / DELETE / draft preview."""

import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.db.models import User, Task
from app.db.session import get_db
from app.deps import get_current_user
from app.inbox import bucket_repo, preview_cache
from app.workers import tasks


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
    # Reclassify existing inbox threads against the new bucket set so the
    # custom bucket can pick up matching threads that are already synced.
    # Async — the user gets 201 immediately, the inbox view updates via the
    # threads_updated SSE event when the worker finishes.
    tasks.reclassify_user_inbox.apply_async(args=[user.id], countdown=0)
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


class _PreviewBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    exclude_thread_ids: list[str] = Field(default_factory=list)


@router.post("/buckets/draft/preview", status_code=202)
def post_draft_preview(body: _PreviewBody, user: User = Depends(get_current_user)) -> dict:
    """Enqueue a draft preview scoring job and return a draft_id.

    Result delivery has two paths the client can use interchangeably:
      - SSE push: a bucket_draft_preview event keyed on draft_id when the
        worker finishes (fast, fire-and-forget — lost if the SSE connection
        blips during the ~40s scoring window).
      - Polling: GET /api/buckets/draft/preview/{draft_id} returns the
        cached result with a 600s TTL. Safety net for SSE delivery loss.
    """
    draft_id = uuid.uuid4().hex
    # mark pending BEFORE enqueueing so the GET endpoint never returns 404
    # to a fast-polling client racing the worker's first redis write.
    preview_cache.mark_pending(draft_id, user_id=user.id)
    tasks.draft_preview_bucket.apply_async(
        args=[user.id, draft_id, body.name, body.description, body.exclude_thread_ids],
        countdown=0,
    )
    return {"draft_id": draft_id}


@router.get("/buckets/draft/preview/{draft_id}")
def get_draft_preview(draft_id: str, response: Response,
                      user: User = Depends(get_current_user)) -> dict:
    """Polling fallback for the SSE-pushed preview result.

      200 + {status:"ready", positives, near_misses} — worker finished.
      202 + {status:"pending"}                       — still scoring.
      404 — unknown draft_id (typo, expired, or never created).
      403 — draft belongs to a different user.
    """
    entry = preview_cache.load(draft_id)
    if entry is None:
        raise HTTPException(404, "not found")
    if entry.get("user_id") != user.id:
        raise HTTPException(403, "not your preview")
    if entry.get("status") == "pending":
        response.status_code = 202
        return {"status": "pending"}
    return {
        "status": "ready",
        "positives": entry.get("positives", []),
        "near_misses": entry.get("near_misses", []),
    }
