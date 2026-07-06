"""GET /api/search — thread text search for the HUD EDA loop.

Response reuses the /api/inbox thread shape so the client renders results
with the existing InboxList row component.
"""

import logging
import time
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.db.models import User
from app.db.session import get_db
from app.deps import get_current_user
from app.api.inbox import _serialize_thread, DEFAULT_LIMIT, MAX_LIMIT
from app.inbox import search_repo

router = APIRouter(prefix="/api", tags=["search"])
log = logging.getLogger(__name__)


@router.get("/search")
def search(
    q: str = Query(min_length=1, max_length=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    page: int = Query(default=1, ge=1),
    include_archived: bool = Query(default=False),
) -> dict:
    offset = (page - 1) * limit
    threads = search_repo.search_threads(
        db, user_id=user.id, q=q, include_archived=include_archived,
        limit=limit, offset=offset)
    log.info("search: user=%s q_len=%d → %d threads", user.id, len(q), len(threads))
    return {
        "as_of": int(time.time() * 1000),
        "page": page,
        "limit": limit,
        "threads": [_serialize_thread(db, user.id, t) for t in threads],
    }
