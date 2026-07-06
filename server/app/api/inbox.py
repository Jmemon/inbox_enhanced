"""Inbox HTTP API.

Four routes:
 - GET  /api/inbox?limit=200&page=N  → paginated thread list with as_of cursor
 - GET  /api/threads/{id}            → single thread fetch (rare; mostly a fallback)
 - POST /api/threads/batch           → fetch many threads in one call (SSE replay path)
 - POST /api/inbox/refresh           → on-demand poll (the Reload button); 202

The batch endpoint is what the client uses after an SSE event delivers a list
of touched thread ids: instead of issuing N parallel GET /api/threads/{id}
calls (which fans out badly on the kickoff full sync, where N can be 200),
the client posts the whole id list and gets one response back.

POST /api/inbox/refresh is the on-demand counterpart to the periodic 30s beat.
The client doesn't need a separate response shape: the existing SSE pipeline
will deliver any updated thread ids exactly the same way as a beat-driven poll.
"""

import logging
import time
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.db.models import User, InboxMessage
from app.db.session import get_db
from app.deps import get_current_user
from app.inbox import inbox_repo
from app.workers import tasks


router = APIRouter(prefix="/api", tags=["inbox"])
log = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_BATCH_IDS = 500


class BatchThreadsRequest(BaseModel):
    thread_ids: list[str] = Field(default_factory=list)


def _serialize_message(msg: InboxMessage | None) -> dict | None:
    if msg is None:
        return None
    return {
        "id": msg.id,
        "gmail_message_id": msg.gmail_id,
        "internal_date": msg.gmail_internal_date,
        "from": msg.from_addr,
        "to": msg.to_addr,
        "body_preview": msg.body_preview,
        "is_unread": msg.is_unread,
    }


def _serialize_thread(db: Session, user_id: str, thread) -> dict:
    # Fetch the recent message scoped to the user — get_message requires user_id
    # so a malicious thread.recent_message_id cannot leak another user's message.
    recent = (
        inbox_repo.get_message(db, user_id=user_id, message_id=thread.recent_message_id)
        if thread.recent_message_id
        else None
    )
    return {
        "id": thread.id,
        "gmail_thread_id": thread.gmail_id,
        "subject": thread.subject,
        "bucket_id": thread.bucket_id,
        "is_archived": thread.is_archived,
        "recent_message": _serialize_message(recent),
    }


@router.get("/inbox")
def list_inbox(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    page: int = Query(default=1, ge=1),
    include_archived: bool = Query(default=False),
) -> dict:
    log.info("list_inbox: user=%s page=%d limit=%d", user.id, page, limit)
    offset = (page - 1) * limit
    threads = inbox_repo.list_threads(
        db, user_id=user.id, limit=limit, offset=offset,
        include_archived=include_archived,
    )
    serialized = [_serialize_thread(db, user.id, t) for t in threads]
    log.info("list_inbox: user=%s → %d threads returned", user.id, len(serialized))
    return {
        "as_of": int(time.time() * 1000),  # ms-precision server timestamp
        "page": page,
        "limit": limit,
        "threads": serialized,
    }


@router.get("/threads/{thread_id}")
def get_thread(
    thread_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    log.info("get_thread: user=%s thread_id=%s", user.id, thread_id)
    t = inbox_repo.get_thread(db, user_id=user.id, thread_id=thread_id)
    if t is None:
        log.info("get_thread: user=%s thread_id=%s → 404", user.id, thread_id)
        raise HTTPException(status_code=404, detail="not found")
    log.info("get_thread: user=%s thread_id=%s → found", user.id, thread_id)
    return _serialize_thread(db, user.id, t)


@router.post("/threads/batch")
def batch_get_threads(
    body: BatchThreadsRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Bulk fetch. Used by the client when a single SSE event names multiple
    thread ids (and especially when the kickoff full-sync delivers 200 ids).

    Unknown ids and ids belonging to other users are silently dropped — the
    response only includes threads the requester owns. The SSE replay path
    relies on this so a stale buffered event doesn't 404 the whole batch.
    """
    log.info("batch_get_threads: user=%s requested=%d ids", user.id, len(body.thread_ids))
    if len(body.thread_ids) > MAX_BATCH_IDS:
        raise HTTPException(status_code=400, detail=f"too many ids (max {MAX_BATCH_IDS})")
    threads = inbox_repo.get_threads_batch(
        db, user_id=user.id, thread_ids=body.thread_ids,
    )
    serialized = [_serialize_thread(db, user.id, t) for t in threads]
    log.info("batch_get_threads: user=%s → %d threads returned", user.id, len(serialized))
    return {"threads": serialized}


class _ExtendBody(BaseModel):
    before_internal_date: int = Field(gt=0)


@router.post("/inbox/extend", status_code=202)
def trigger_extend(body: _ExtendBody, user: User = Depends(get_current_user)) -> JSONResponse:
    """Kick off an on-demand extend for threads older than before_internal_date (ms).

    The result arrives via SSE as an extend_complete event — the client does not
    poll this endpoint for data. Returns 202 immediately after enqueuing the task.
    """
    tasks.extend_inbox_history_task.apply_async(
        args=[user.id, body.before_internal_date], countdown=0,
    )
    return JSONResponse({"ok": True}, status_code=202)


@router.post("/inbox/refresh", status_code=202)
def trigger_refresh(user: User = Depends(get_current_user)) -> JSONResponse:
    """On-demand poll. Mirrors the kickoff path in the SSE endpoint:
    full_sync if the user has no history cursor yet, else partial.

    The client doesn't get the result here — it'll arrive via SSE the same way
    a beat-driven poll would.
    """
    if user.gmail_last_history_id:
        log.info("trigger_refresh: user=%s → enqueuing poll_new_messages (partial)", user.id)
        tasks.poll_new_messages.apply_async(args=[user.id], countdown=0)
    else:
        log.info("trigger_refresh: user=%s → enqueuing full_sync_inbox_task (no history cursor)", user.id)
        tasks.full_sync_inbox_task.apply_async(args=[user.id], countdown=0)
    return JSONResponse({"ok": True}, status_code=202)
