"""GET /api/sync/status — feeds the HUD freshness strip.

last_synced_at: epoch seconds of the user's last successful sync task
(null before first sync or if Redis lost the marker — the client renders
"never" / "unknown"). has_cursor: whether incremental sync is established.
"""

from fastapi import APIRouter, Depends
from app.db.models import User
from app.deps import get_current_user
from app.realtime import last_sync

router = APIRouter(prefix="/api", tags=["sync"])


@router.get("/sync/status")
def sync_status(user: User = Depends(get_current_user)) -> dict:
    return {
        "last_synced_at": last_sync.get(user.id),
        "has_cursor": bool(user.gmail_last_history_id),
    }
