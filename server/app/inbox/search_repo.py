"""Thread text search. NEVER commits (caller owns the txn).

Two branches on dialect:
 - postgresql: FTS over the 0006 generated tsvector columns
   (inbox_messages.search_tsv, inbox_threads.subject_tsv) ranked by
   ts_rank_cd then recency. websearch_to_tsquery gives users quotes/-/OR.
 - everything else (SQLite tests): ILIKE substring over subject / sender /
   body_text, recency-ordered. Same contract, no ranking.
"""

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session
from app.db.models import InboxMessage, InboxThread


def search_threads(
    db: Session, *, user_id: str, q: str,
    include_archived: bool = False, limit: int = 50, offset: int = 0,
) -> list[InboxThread]:
    if db.get_bind().dialect.name == "postgresql":
        return _search_pg(db, user_id=user_id, q=q,
                          include_archived=include_archived, limit=limit, offset=offset)
    return _search_fallback(db, user_id=user_id, q=q,
                            include_archived=include_archived, limit=limit, offset=offset)


def _search_pg(db, *, user_id, q, include_archived, limit, offset) -> list[InboxThread]:
    rows = db.execute(text("""
        SELECT t.id
        FROM inbox_threads t
        LEFT JOIN inbox_messages m
               ON m.thread_id = t.id AND m.is_deleted = false
        WHERE t.user_id = :user_id
          AND (:include_archived OR t.is_archived = false)
          AND (t.subject_tsv @@ websearch_to_tsquery('english', :q)
               OR m.search_tsv @@ websearch_to_tsquery('english', :q)
               OR m.from_addr ILIKE '%' || :q || '%')
        GROUP BY t.id, t.last_activity_at
        ORDER BY COALESCE(MAX(ts_rank_cd(m.search_tsv,
                                         websearch_to_tsquery('english', :q))), 0) DESC,
                 t.last_activity_at DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """), {"user_id": user_id, "q": q, "include_archived": include_archived,
           "limit": limit, "offset": offset}).all()
    ids = [r[0] for r in rows]
    if not ids:
        return []
    by_id = {t.id: t for t in db.execute(
        select(InboxThread).where(InboxThread.id.in_(ids))).scalars()}
    return [by_id[i] for i in ids if i in by_id]  # preserve rank order


def _search_fallback(db, *, user_id, q, include_archived, limit, offset) -> list[InboxThread]:
    like = f"%{q}%"
    stmt = (
        select(InboxThread).distinct()
        .outerjoin(InboxMessage, (InboxMessage.thread_id == InboxThread.id)
                   & (InboxMessage.is_deleted == False))  # noqa: E712
        .where(InboxThread.user_id == user_id)
        .where(or_(InboxThread.subject.ilike(like),
                   InboxMessage.from_addr.ilike(like),
                   InboxMessage.body_text.ilike(like)))
        .order_by(InboxThread.last_activity_at.desc().nulls_last())
        .limit(limit).offset(offset)
    )
    if not include_archived:
        stmt = stmt.where(InboxThread.is_archived == False)  # noqa: E712
    return list(db.execute(stmt).scalars().all())
