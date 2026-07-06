"""Per-user last-successful-sync marker (Redis, 90-day self-expiring TTL).

Written by the sync Celery tasks on every successful completion; read by
GET /api/sync/status to power the HUD freshness strip ("synced 12s ago").
Deliberately a marker, not a log — one key per user, overwritten in place.
"""

import time
from app.realtime import redis_client

# Refreshed on every mark() for an active user, so in practice this never
# expires for anyone who keeps syncing. It only kicks in for a user who
# stops syncing entirely (account abandoned/deleted, Gmail access revoked)
# — without it, `last_sync:{uid}` would otherwise live in Redis forever,
# one unbounded key per user who ever synced. 90 days comfortably outlives
# any real gap between syncs while still being a self-cleaning ceiling.
TTL_SECONDS = 60 * 60 * 24 * 90


def _key(user_id: str) -> str:
    return f"last_sync:{user_id}"


def mark(user_id: str) -> None:
    redis_client.get_redis().set(_key(user_id), int(time.time()), ex=TTL_SECONDS)


def get(user_id: str) -> int | None:
    v = redis_client.get_redis().get(_key(user_id))
    return int(v) if v is not None else None
