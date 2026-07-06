"""Per-draft-id cache for task-propose draft results.

Mirrors `app.inbox.preview_cache`'s contract verbatim (see that module for
the polling-fallback rationale), for the goal -> proposed-schema/criteria
draft flow instead of the bucket draft-preview flow: `POST /api/tasks/draft`
(a later task) enqueues `workers.task_engine_tasks.propose_task_draft` and
writes a pending placeholder here immediately after; `GET
/api/tasks/draft/{draft_id}` polls this cache if the SSE push
(`task_draft_ready`) was lost.

Stored shape:
  pending: {"status": "pending", "user_id": "<uid>"}
  ready:   {"status": "ready",   "user_id": "<uid>", **payload}

`payload` is caller-defined (`task_engine_tasks.propose_task_draft` supplies
`{"proposal": {...}, "positives": [...], "near_misses": [...]}`) -- this
module doesn't know or care about its shape, same as preview_cache.

user_id is stored so the GET endpoint can verify ownership -- draft_id is a
uuid hex generated server-side but we don't want to assume it's a secret.

TTL is 600s, matching preview_cache/sync_lock -- comfortably longer than
worst-case propose+score latency plus a buffer for the user to review.
"""

import json
from app.realtime import redis_client


_KEY_PREFIX = "task_draft"
_DEFAULT_TTL_SECONDS = 600


def _key(draft_id: str) -> str:
    return f"{_KEY_PREFIX}:{draft_id}"


def mark_pending(draft_id: str, *, user_id: str,
                 ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
    """Write a pending placeholder. Called from the POST endpoint right after
    enqueuing the celery task so a polling client immediately sees
    status=pending instead of racing the worker startup and seeing 404."""
    body = json.dumps({"status": "pending", "user_id": user_id})
    redis_client.get_redis().set(_key(draft_id), body, ex=ttl_seconds)


def store_result(draft_id: str, *, user_id: str, payload: dict,
                 ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
    """Overwrite the pending placeholder with the worker's result. Called
    BEFORE the SSE publish so a poll that lands between the cache write and
    the dispatch sees the ready payload instead of stale pending."""
    body = json.dumps({"status": "ready", "user_id": user_id, **payload})
    redis_client.get_redis().set(_key(draft_id), body, ex=ttl_seconds)


def load(draft_id: str) -> dict | None:
    """Read the cached entry. Returns the parsed dict or None for missing
    or expired keys (TTL elapsed). 404 on the wire."""
    raw = redis_client.get_redis().get(_key(draft_id))
    if raw is None:
        return None
    return json.loads(raw)
