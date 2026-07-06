"""Inbox read/write helpers shared by api endpoints and celery workers.

All functions take a SQLAlchemy Session that the caller owns; this module
never commits. The caller (api request handler or worker task) controls the
transaction boundary so a sync job can write threads + messages + history-id
update atomically.
"""

import uuid
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from app.db.models import InboxMessage, InboxThread, User


def upsert_thread(
    db: Session,
    *,
    user_id: str,
    gmail_thread_id: str,
    subject: str | None,
    bucket_id: str | None,
) -> InboxThread:
    """Upsert one thread row by (user_id, gmail_thread_id).

    Concurrent inserts of the same (user_id, gmail_thread_id) will raise
    sqlalchemy.exc.IntegrityError on the second write — the unique constraint
    catches the SELECT-then-INSERT race. Worker tasks (workers/tasks.py) handle
    that by letting Celery retry the entire task; this module never catches.
    """
    stmt = select(InboxThread).where(
        InboxThread.user_id == user_id,
        InboxThread.gmail_id == gmail_thread_id,
    )
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        row = InboxThread(
            id=uuid.uuid4().hex,
            user_id=user_id,
            gmail_id=gmail_thread_id,
            subject=subject,
            bucket_id=bucket_id,
            recent_message_id=None,
        )
        db.add(row)
        db.flush()
    else:
        row.subject = subject if subject is not None else row.subject
        if bucket_id is not None:
            row.bucket_id = bucket_id
    return row


def upsert_message(
    db: Session,
    *,
    user_id: str,
    gmail_thread_id: str,
    gmail_message_id: str,
    gmail_internal_date: int,
    gmail_history_id: str,
    to_addr: str | None,
    from_addr: str | None,
    body_preview: str | None,
    body_text: str | None = None,
    label_ids: list[str] | None = None,
) -> InboxMessage:
    # Thread must already exist (caller is expected to upsert_thread first).
    thread = db.execute(
        select(InboxThread).where(
            InboxThread.user_id == user_id, InboxThread.gmail_id == gmail_thread_id
        )
    ).scalar_one()

    existing = db.execute(
        select(InboxMessage).where(
            InboxMessage.user_id == user_id,
            InboxMessage.gmail_id == gmail_message_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = InboxMessage(
            id=uuid.uuid4().hex,
            thread_id=thread.id,
            user_id=user_id,
            gmail_id=gmail_message_id,
            gmail_thread_id=gmail_thread_id,
            gmail_internal_date=gmail_internal_date,
            gmail_history_id=gmail_history_id,
            to_addr=to_addr,
            from_addr=from_addr,
            body_preview=body_preview,
            body_text=body_text,
            labels=list(label_ids or []),
            is_unread="UNREAD" in (label_ids or []),
        )
        db.add(existing)
        db.flush()
    else:
        existing.gmail_internal_date = gmail_internal_date
        existing.gmail_history_id = gmail_history_id
        existing.to_addr = to_addr
        existing.from_addr = from_addr
        existing.body_preview = body_preview
        existing.body_text = body_text if body_text is not None else existing.body_text
        if label_ids is not None:
            existing.labels = list(label_ids)
            existing.is_unread = "UNREAD" in label_ids

    recompute_thread_pointers(db, thread=thread)
    return existing


def recompute_thread_pointers(db: Session, *, thread: InboxThread) -> None:
    """Recompute recent_message_id + last_activity_at from the thread's
    non-deleted messages. Single indexed lookup (gmail_internal_date is
    indexed). Soft-deleted messages are invisible to both pointers so a
    Gmail deletion demotes the thread's sort position instead of pinning it.

    Flushes first: sessions here run with autoflush=False (see db/session.py),
    and callers are expected to mutate InboxMessage.is_deleted on an
    already-loaded ORM object (e.g. a future soft-delete task) then call this
    helper directly — without an explicit flush the SELECT below would still
    see the pre-mutation row."""
    db.flush()
    row = db.execute(
        select(InboxMessage.id, InboxMessage.gmail_internal_date)
        .where(InboxMessage.thread_id == thread.id,
               InboxMessage.is_deleted == False)  # noqa: E712
        .order_by(InboxMessage.gmail_internal_date.desc())
        .limit(1)
    ).first()
    if row is None:
        thread.recent_message_id = None
        thread.last_activity_at = None
    else:
        thread.recent_message_id = row[0]
        thread.last_activity_at = row[1]


def list_threads(
    db: Session, *, user_id: str, limit: int, offset: int,
    include_archived: bool = False,
) -> list[InboxThread]:
    """Threads for the user, most-recently-active first (indexed
    last_activity_at sort — no join). Archived threads are hidden unless
    asked for; they remain queryable because tasks may reference them.

    Read-only: this is a pure SELECT and must not flush. Sessions here run
    with autoflush=False (see db/session.py); callers that mutate an
    already-loaded ORM object (e.g. thread.is_archived) and need that write
    visible to this SELECT are responsible for flushing/committing before
    calling list_threads — see recompute_thread_pointers for the analogous
    write-side helper that does flush."""
    stmt = (
        select(InboxThread)
        .where(InboxThread.user_id == user_id)
        .order_by(InboxThread.last_activity_at.desc().nulls_last())
        .limit(limit)
        .offset(offset)
    )
    if not include_archived:
        stmt = stmt.where(InboxThread.is_archived == False)  # noqa: E712
    return list(db.execute(stmt).scalars().all())


def get_thread(db: Session, *, user_id: str, thread_id: str) -> InboxThread | None:
    return db.execute(
        select(InboxThread).where(
            InboxThread.id == thread_id, InboxThread.user_id == user_id
        )
    ).scalar_one_or_none()


def get_threads_batch(
    db: Session, *, user_id: str, thread_ids: list[str]
) -> list[InboxThread]:
    """Returns the InboxThread rows for the given ids, scoped to user_id.

    Threads not owned by user_id (or non-existent) are silently omitted; result
    order is NOT guaranteed to match thread_ids — caller sorts client-side using
    the id layer that already encodes the desired order.
    """
    if not thread_ids:
        return []
    stmt = select(InboxThread).where(
        InboxThread.user_id == user_id,
        InboxThread.id.in_(thread_ids),
    )
    return list(db.execute(stmt).scalars().all())


def get_message(db: Session, *, user_id: str, message_id: str) -> InboxMessage | None:
    """Fetch one message scoped to a user. Returns None if no such message
    exists OR if the message belongs to a different user — callers are NOT
    told which case occurred (no enumeration via 404 vs 403 split)."""
    return db.execute(
        select(InboxMessage).where(
            InboxMessage.id == message_id,
            InboxMessage.user_id == user_id,
        )
    ).scalar_one_or_none()


def update_user_history_id(db: Session, *, user_id: str, history_id: str) -> None:
    user = db.get(User, user_id)
    if user is not None:
        user.gmail_last_history_id = history_id


def clear_user_inbox(db: Session, *, user_id: str) -> None:
    """Wipe all inbox_threads + inbox_messages for one user.

    Used only for account deletion. Sync paths must never call this — task
    evidence FKs onto these rows. Order matters — messages have a FK to
    threads.
    """
    db.execute(delete(InboxMessage).where(InboxMessage.user_id == user_id))
    db.execute(delete(InboxThread).where(InboxThread.user_id == user_id))
