<!-- stamp: 13a07e5 (main) | 2026-05-29 -->

# Workers Index

> Scope: Celery worker + beat subsystem — celery_app factory (broker/backend=REDIS_URL, task_always_eager test mode), 30s beat tick, tasks (enqueue_polls, poll_new_messages, full_sync_inbox_task, extend_inbox_history_task, draft_preview_bucket, reclassify_user_inbox), gmail_sync orchestration entrypoint, per-user sync_lock, active_users zset gating, cache-before-publish, pubsub user:{uid} SSE fan-out.

## Files
| Path | Role / key exports |
|------|--------------------|
| server/app/workers/celery_app.py | `_build_app()` → `celery_app` (Celery singleton). broker=backend=`settings.redis_url`; `include=["app.workers.tasks"]`; json serializer, UTC, `task_acks_late=False`, `broker_connection_retry_on_startup=True`. Enables `task_always_eager`+`task_eager_propagates` when env `CELERY_TASK_ALWAYS_EAGER=="1"`. Imports + wires `beat_schedule`. Own `logging.basicConfig` (separate process from API). **WORKING-TREE MOD: app name `inbox_concierge`→`inbox_enhanced` (uncommitted).** |
| server/app/workers/beat_schedule.py | `beat_schedule` dict. One entry `enqueue-polls-every-30s` → task `app.workers.tasks.enqueue_polls`, `schedule(run_every=30.0)`. No polling logic in beat. |
| server/app/workers/tasks.py | 6 `@celery_app.task`s (below) + helpers `_publish`, `_publish_thread_ids`, `_read_candidates`, `_extend_inline`, `_score_all`, `_inline_reload`, `_reclassify_all`. Module-level `SessionLocal` (rebindable for tests). Constants: `CANDIDATE_LIMIT=100`, `EXTEND_THRESHOLD=100`, `TOP_POSITIVES=3`, `TOP_NEAR_MISSES=3`, `POSITIVE_THRESHOLD=7`, `NEAR_MISS_LOW=4`, `NEAR_MISS_HIGH=6`. |
| server/app/workers/gmail_sync.py | Worker-internal sync orchestration (Celery-task entrypoint role only; deep history-cursor/parser internals → future GMAIL_SYNC_INDEX.md). Exports `HistoryGoneError`, `fetch_history_records`, `partial_sync_inbox`, `full_sync_inbox`, `extend_inbox_history` + privates `_upsert_thread_with_messages`, `_classify_batch`. All public fns commit internally + return internal `InboxThread.id` lists (not gmail ids). |

## Routes / Tasks / Entrypoints

### Celery tasks (all `app.workers.tasks.*`)
| Task (name) | Signature | Trigger | Behavior (one line) |
|------|-----------|---------|---------|
| `enqueue_polls` | `()` | beat tick (30s) | `active_users.purge_expired()` then `poll_new_messages.apply_async([uid], countdown=0)` per active uid. Fan-out only. |
| `poll_new_messages` | `(user_id)` | beat fan-out; API `/sse` kickoff (cursor exists), `/inbox/refresh` (cursor exists) | acquire `sync_lock` (else skip); no cursor→`full_sync_inbox`; else `fetch_history_records`→404=`HistoryGoneError`→full; empty→silent (no publish); records→`partial_sync_inbox`; publish `threads_updated`. |
| `full_sync_inbox_task` | `(user_id)` | API `/sse` kickoff (no cursor), `/inbox/refresh` (no cursor) | lock→`gmail_sync.full_sync_inbox`→publish `threads_updated`. |
| `extend_inbox_history_task` | `(user_id, before_internal_date_ms)` | API `POST /inbox/extend` (202) | lock→`gmail_sync.extend_inbox_history`→publish `extend_complete {thread_ids, more}` (more=True when 200 stubs returned). |
| `draft_preview_bucket` | `(user_id, draft_id, name, description, exclude_thread_ids=None)` | API `POST /buckets/draft/preview` (202) | read ≤100 candidates; pool<100→`_extend_inline` (takes lock); `_score_all` (seq gmail get + parallel LLM); top-3 ≥7 / top-3 4–6; **`preview_cache.store_result` BEFORE** publish `bucket_draft_preview`. NO sync_lock held for the whole task. |
| `reclassify_user_inbox` | `(self, user_id)` `bind=True, max_retries=3` | API `POST /buckets` (custom bucket created) | lock (contended→`self.retry(countdown=30)`); `_inline_reload`(partial/full) + `_reclassify_all` (refetch all, classify w/ current bucket as stability hint, write only changed); publish union via `threads_updated`. |

### Beat entries
| Key | Schedule | Enqueues |
|-----|----------|----------|
| `enqueue-polls-every-30s` | `schedule(run_every=30.0)` | `enqueue_polls` onto broker every 30s. |

### gmail_sync orchestration entrypoints (called by tasks, not Celery tasks themselves)
- `fetch_history_records(gmail, *, start_history_id)` → `(records, latest_history_id)`; `users.history.list(historyTypes=[messageAdded], labelId="INBOX")`; 404→`HistoryGoneError`.
- `partial_sync_inbox(db, *, user, history_records=None, new_history_id=None)` → internal ids; incremental writer; per-thread try/except tolerates deleted-thread 404s so cursor advances.
- `full_sync_inbox(db, *, user)` → internal ids; `clear_user_inbox`→`threads.list(maxResults=200, labelIds=["INBOX"])`→per-stub get→classify→upsert→advance cursor to max history_id.
- `extend_inbox_history(db, *, user, before_internal_date_ms)` → `(ids, more)`; `threads.list(q=before:<unix_secs>, labelIds=["INBOX"])`; does NOT touch `gmail_last_history_id`; caller owns lock.

## Data & state touched

### Postgres (via own `SessionLocal()`, not `Depends(get_db)`) — §2.3
| Table | R/W |
|-------|-----|
| `User` (`gmail_last_history_id`, OAuth tokens) | R cursor; W cursor via `inbox_repo.update_user_history_id` |
| `InboxThread` (`id`, `gmail_id`, `subject`, `bucket_id`, `recent_message_id`) | R candidates/rows; W upsert + reclassify `bucket_id` |
| `InboxMessage` (`from_addr`, `body_preview`, `gmail_internal_date`, `gmail_history_id`) | R for candidates/oldest cursor; W upsert; `clear_user_inbox` deletes (msgs→threads FK order) |
| `Bucket` | R via `bucket_repo.list_active` |

### Redis (REDIS_URL — broker + result backend + state, one instance) — §2.7
| Key / channel | Op | Source |
|------|-----|--------|
| celery broker queue | LPUSH (`apply_async`) / BRPOP (worker) | enqueue paths + beat |
| celery result backend | task result store | celery_app `backend` |
| `active_users` (zset, KEY=`active_users`, score=expiry epoch) | ZRANGE `list_active`, ZREMRANGEBYSCORE `purge_expired` | `enqueue_polls` (R/W) |
| `sync_lock:{uid}` (`SET nx ex 600`) | acquire (NX) / DEL release | poll/full/extend/reclassify + `_extend_inline` |
| `preview:{draft_id}` (`SET ex 600`) | `mark_pending` (API), `store_result` (worker) | `draft_preview_bucket` (W) |
| `user:{uid}` (pubsub) | PUBLISH | `_publish` (all publishers); logs subscriber count |

### External services
- Gmail v1 (`get_gmail_client`; Fernet-decrypt refresh token per call) — §2.9: `users.history.list / threads.list / threads.get(format=full)`.
- Anthropic Messages API — §2.10: via `llm_client.call_messages` under shared `Semaphore(ANTHROPIC_CONCURRENCY)` on the worker's LLM loop thread; `classify` (sync paths) + `score_thread` (preview).

### Env-var names (from app/config.py — do not read .env)
`REDIS_URL`, `DATABASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_CLASSIFY_MODEL` (default `claude-haiku-4-5`), `ANTHROPIC_CONCURRENCY` (default 16). Process env (not config): `CELERY_TASK_ALWAYS_EAGER`.

## Data flows / cross-subsystem touchpoints
```
Beat ──[30s: LPUSH enqueue_polls]──> Redis ──[BRPOP]──> Worker.enqueue_polls          (§2.6)
  enqueue_polls ──[ZRANGE active_users]──> uids
  enqueue_polls ──[apply_async poll_new_messages(uid)]──> Redis ──[BRPOP]──> Worker    (§2.6)

API.GET /sse        ──[apply_async]──> poll_new_messages(uid)      (cursor present)     (§2.5)
API.GET /sse        ──[apply_async]──> full_sync_inbox_task(uid)   (no cursor)          (§2.5)
API.POST /inbox/refresh ──[apply_async]──> poll_new_messages | full_sync_inbox_task     (§2.5)
API.POST /inbox/extend  ──[apply_async]──> extend_inbox_history_task(uid, before_ms)    (§2.5)
API.POST /buckets/draft/preview ─[mark_pending preview:{id}; apply_async]→ draft_preview_bucket
API.POST /buckets   ──[apply_async]──> reclassify_user_inbox(uid)                       (§2.5)

Worker.* ──[SET sync_lock:{uid} NX EX 600]──> Redis
Worker (sync) ──[history.list/threads.list/threads.get + token refresh]──> Gmail v1    (§2.9)
Worker (LLM thread) ──[POST messages, semaphore-bounded]──> api.anthropic.com          (§2.10)
draft_preview_bucket ──[SET preview:{draft_id} EX 600]──> Redis  (BEFORE next line)
Worker ──[PUBLISH user:{uid} {event,...}]──> Redis ──> API PubSubDispatcher ──[SSE]──> Browser  (§2.7)
```
Published events → browser over SSE: `threads_updated {thread_ids}` (poll/full/reclassify), `extend_complete {thread_ids, more}` (extend), `bucket_draft_preview {draft_id, positives, near_misses}` (preview). Browser then re-reads via `/api/threads/batch` (filters by `InboxThread.id` — why workers return internal ids, not gmail ids). Preview also pollable via `GET /api/buckets/draft/preview/{draft_id}` (preview cache fallback).

## Decision points & gotchas
- **sync_lock serialization**: poll / full / extend / reclassify acquire `sync_lock:{uid}` (NX, 600s TTL) to avoid racing the `(user_id, gmail_id)` unique constraint. Contended → poll/full/extend skip silently; `reclassify_user_inbox` instead `self.retry(countdown=30)`. `draft_preview_bucket` does NOT hold it across the task — only `_extend_inline` grabs it transiently (skips if held).
- **active-user gating**: only uids in the `active_users` zset (added by `/sse` on first connect, TTL 60s, refreshed) get polled. `enqueue_polls` purges expired before listing — disconnected users stop being polled.
- **ack semantics**: `task_acks_late=False` (ack on receipt, not completion) — periodic poll is best-effort; a worker crash mid-task loses that run (next beat tick re-polls).
- **cache-before-publish ordering**: `draft_preview_bucket` calls `preview_cache.store_result` BEFORE `_publish` so a client polling between the two reads the ready result, not stale "pending". `mark_pending` is written by the API at enqueue time so the GET never 404s a fast poller.
- **HistoryGoneError / cursor expiry**: `fetch_history_records` 404 (cursor older than ~30-day window) → fall back to `full_sync_inbox` (wipe + repopulate 200). Same fallback in `poll_new_messages` and `_inline_reload`. `extend_inbox_history` deliberately leaves cursor untouched (must stay anchored at newest message).
- **noise filter**: empty history records → `poll_new_messages` returns without publishing (no `subscribers=0` churn).
- **score thresholds (preview)**: positives `score >= 7` (top 3), near-misses `4 <= score <= 6` (top 3).
- **INBOX scoping**: `labelId="INBOX"` (singular, history.list) / `labelIds=["INBOX"]` (plural, threads.list) — without it SENT/DRAFTS leak into the inbox table.
- **beat single-replica**: beat service `numReplicas=1` (multiple beats multiply Gmail fan-out). Beat owns no app code beyond `beat_schedule.py`.
- **eager mode**: `CELERY_TASK_ALWAYS_EAGER=1` (tests) runs tasks synchronously in-process with `task_eager_propagates=True`; `SessionLocal` is module-rebindable for in-memory engines.
- **alembic NOT run by worker/beat**: Dockerfile chains `alembic upgrade head` before uvicorn, but worker/beat override CMD — only the API container migrates.
- **fixed countdown=0**: `enqueue_polls` enqueues with `countdown=0` for test determinism (the docstring notes prod could randomize a 0–10s spread).
