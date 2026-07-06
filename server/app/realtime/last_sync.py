"""Per-user last-successful-sync marker (Redis, no TTL).

Written by the sync Celery tasks on every successful completion; read by
GET /api/sync/status to power the HUD freshness strip ("synced 12s ago").
Deliberately a marker, not a log — one key per user, overwritten in place.
"""

import time
from app.realtime import redis_client


def _key(user_id: str) -> str:
    return f"last_sync:{user_id}"


def mark(user_id: str) -> None:
    redis_client.get_redis().set(_key(user_id), int(time.time()))


def get(user_id: str) -> int | None:
    v = redis_client.get_redis().get(_key(user_id))
    return int(v) if v is not None else None
