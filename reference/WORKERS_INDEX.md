<!-- stamp: 00d736e (feature/phase0-data-floor) | 2026-07-05 -->

# Workers Index

> Scope: Celery worker + beat subsystem — celery_app factory (broker/backend=REDIS_URL, task_always_eager test mode), 30s beat tick, tasks (enqueue_polls, poll_new_messages, full_sync_inbox_task, extend_inbox_history_task, draft_preview_bucket, reclassify_user_inbox), gmail_sync orchestration entrypoint (reconciling full sync, widened-historyTypes partial sync), per-user sync_lock, active_users zset gating, cache-before-publish, pubsub user:{uid} SSE fan-out, LLM via OpenRouter with per-call llm_calls metrics persistence (app/llm/metrics.py).

## Files
| Path | Role / key exports |
|------|--------------------|
| server/app/workers/celery_app.py | `_build_app()` → `celery_app` (Celery singleton). broker=backend=`settings.redis_url`; `include=["app.workers.tasks"]`; json serializer, UTC, `task_acks_late=False`, `broker_connection_retry_on_startup=True`. Enables `task_always_eager`+`task_eager_propagates` when env `CELERY_TASK_ALWAYS_EAGER=="1"`. Imports + wires `beat_schedule`. Own `logging.basicConfig` (separate process from API). App name `inbox_enhanced`. |
| server/app/workers/beat_schedule.py | `beat_schedule` dict. One entry `enqueue-polls-every-30s` → task `app.workers.tasks.enqueue_polls`, `schedule(run_every=30.0)`. No polling logic in beat. |
| server/app/workers/tasks.py | 6 `@celery_app.task`s (below) + helpers `_publish`, `_publish_thread_ids`, `_read_candidates`, `_extend_inline`, `_score_all(db, *, user_id, candidates, name, description)`, `_inline_reload`, `_reclassify_all(db, *, user)`. Module-level `SessionLocal` (rebindable for tests). Constants: `CANDIDATE_LIMIT=100`, `EXTEND_THRESHOLD=100`, `TOP_POSITIVES=3`, `TOP_NEAR_MISSES=3`, `POSITIVE_THRESHOLD=7`, `NEAR_MISS_LOW=4`, `NEAR_MISS_HIGH=6`. |
| server/app/workers/gmail_sync.py | Worker-internal sync orchestration (Celery-task entrypoint role only; deep history-cursor/parser internals → `INBOX_SYNC_INDEX.md`). Exports `HistoryGoneError`, `fetch_history_records`, `partial_sync_inbox`, `full_sync_inbox`, `extend_inbox_history` + privates `_upsert_thread_with_messages`, `_classify_batch`. All public fns commit internally and return internal `InboxThread.id` lists (not gmail ids). `full_sync_inbox` is a **reconciling upsert** (archives/un-archives, never deletes); `partial_sync_inbox` handles 4 history-record shapes (messagesAdded/Deleted, labelsAdded/Removed for INBOX+UNREAD). |
| server/app/inbox/inbox_repo.py | Postgres read/write for threads/messages (deep detail → `INBOX_SYNC_INDEX.md`). New in Phase 0: `recompute_thread_pointers` (write-path, flushes), `list_threads(..., include_archived=False)` sorted by `last_activity_at`, `load_parsed_threads(db, *, user_id, internal_ids=None)` — rebuilds `ParsedThread`s from stored rows (body_text/labels), used by `_reclassify_all`/`_score_all` instead of a Gmail refetch. `clear_user_inbox` is account-deletion-only, uncalled by workers. |
| server/app/llm/client.py | `call_messages(*, model, system, user, max_tokens=1024, stage="unknown", user_id=None) -> str` — one `AsyncOpenAI.chat.completions.create` call under the shared `Semaphore(LLM_CONCURRENCY)` on the dedicated `llm-loop` background thread; `extra_body={"usage":{"include": True}}` asks OpenRouter to attach cost + cached-token counts to `resp.usage`. Records exactly one `llm_calls` row per call via `metrics.record_call` (fire-and-forget, `asyncio.to_thread`) — success row built from `resp.usage` (input/output/cache_read tokens, cost) AFTER content is extracted (so a malformed 200 with empty `choices`/`message=None` raises into the `except` and records an `outcome="error"` row instead of double-counting); returns `""` on any exception so a per-thread classify/score failure degrades to no-fit rather than crashing the whole batch. `run_in_loop(coro)` — sync→async bridge for Celery callers. `_ensure_initialized()` lazy-inits the loop/thread/semaphore/client once per process (fork-safe). `reset_for_tests()`. |
| server/app/llm/metrics.py | `record_call(*, stage, model, user_id=None, task_id=None, input_tokens=None, output_tokens=None, cache_read_tokens=None, cache_write_tokens=None, cost_usd=None, ttft_ms=None, duration_ms, outcome)` — writes one `LlmCall` row. Deliberately fire-and-forget: own short-lived `SessionLocal()` (module-attr, monkeypatchable like `workers/tasks.py`), wraps the whole body in `try/except Exception: log.exception(...)` — a metrics-write failure must never fail (or retry) the LLM call it's recording. Called from `llm/client.py` via `asyncio.to_thread` so the sync DB write never blocks the async LLM event loop. |
| server/app/llm/classify.py | `classify(threads, buckets, current_bucket_ids, *, user_id=None) -> list[str \| None]` — one call per thread in parallel via `asyncio.gather` under the semaphore (through `client.run_in_loop`); output order matches input order. `_classify_one` passes `stage="classify", user_id=user_id` into `call_messages` for metrics attribution; no-fit response falls through to `current_bucket_id` (keeps existing assignment, `None` for new threads). |
| server/app/db/models.py | `LlmCall` (`server/app/db/models.py`, table `llm_calls`) — `id, user_id, task_id, stage, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, ttft_ms, duration_ms, outcome, created_at`; indexed on `user_id` and `created_at`. Also carries the Phase-0 `InboxThread.is_archived`/`last_activity_at` and `InboxMessage.body_text`/`labels`/`is_unread`/`is_deleted` columns the sync/reclassify/preview paths read+write (full detail → `INBOX_SYNC_INDEX.md`). |

## Routes / Tasks / Entrypoints

### Celery tasks (all `app.workers.tasks.*`)
| Task (name) | Signature | Trigger | Behavior (one line) |
|------|-----------|---------|---------|
| `enqueue_polls` | `()` | beat tick (30s) | `active_users.purge_expired()` then `poll_new_messages.apply_async([uid], countdown=0)` per active uid. Fan-out only. |
| `poll_new_messages` | `(user_id)` | beat fan-out; API `/sse` kickoff (cursor exists), `/inbox/refresh` (cursor exists) | acquire `sync_lock` (else skip); no cursor→`full_sync_inbox`; else `fetch_history_records`→404=`HistoryGoneError`→full; empty→silent (no publish); records→`partial_sync_inbox`; publish `threads_updated`. |
| `full_sync_inbox_task` | `(user_id)` | API `/sse` kickoff (no cursor), `/inbox/refresh` (no cursor) | lock→`gmail_sync.full_sync_inbox` (reconciling upsert: archives/un-archives, never deletes)→publish `threads_updated`. |
| `extend_inbox_history_task` | `(user_id, before_internal_date_ms)` | API `POST /inbox/extend` (202) | lock→`gmail_sync.extend_inbox_history`→publish `extend_complete {thread_ids, more}` (more=True when 200 stubs returned). |
| `draft_preview_bucket` | `(user_id, draft_id, name, description, exclude_thread_ids=None)` | API `POST /buckets/draft/preview` (202) | read ≤100 candidates; pool<100→`_extend_inline` (takes lock); `_score_all` (Postgres bodies via `load_parsed_threads` + parallel LLM — **no Gmail refetch**); top-3 ≥7 / top-3 4–6; **`preview_cache.store_result` BEFORE** publish `bucket_draft_preview`. NO sync_lock held for the whole task. |
| `reclassify_user_inbox` | `(self, user_id)` `bind=True, max_retries=3` | API `POST /buckets` (custom bucket created) | lock (contended→`self.retry(countdown=30)`); `_inline_reload`(partial/full) + `_reclassify_all` (Postgres bodies via `load_parsed_threads`, **no Gmail refetch** — classify w/ current bucket as stability hint, write only changed); publish union via `threads_updated`. |

### Beat entries
| Key | Schedule | Enqueues |
|-----|----------|----------|
| `enqueue-polls-every-30s` | `schedule(run_every=30.0)` | `enqueue_polls` onto broker every 30s. |

### gmail_sync orchestration entrypoints (called by tasks, not Celery tasks themselves)
- `fetch_history_records(gmail, *, start_history_id)` → `(records, latest_history_id)`; `users.history.list(historyTypes=["messageAdded","messageDeleted","labelAdded","labelRemoved"], labelId="INBOX")`; 404→`HistoryGoneError`.
- `partial_sync_inbox(db, *, user, history_records=None, new_history_id=None)` → internal ids; incremental writer over 4 record shapes: `messagesAdded` (fetch+classify+upsert), `messagesDeleted` (soft-delete `is_deleted` + `recompute_thread_pointers` + archive-when-empty), `labelsAdded`/`labelsRemoved` INBOX (archive/un-archive, or ingest an unseen thread on INBOX-added) and UNREAD (flip `is_unread`); per-thread try/except tolerates deleted-thread 404s so cursor advances.
- `full_sync_inbox(db, *, user)` → internal ids; **reconciling upsert, not a wipe**: `threads.list(maxResults=200, labelIds=["INBOX"])` → per-stub get → classify → upsert → un-archive any stored thread the listing returns → archive stored non-archived threads inside the listing's activity window but absent from it (window floor guarded against `recent_internal_date=0` messageless entries) → advance cursor to max history_id. `clear_user_inbox` is NOT called here (account-deletion-only, currently uncalled anywhere).
- `extend_inbox_history(db, *, user, before_internal_date_ms)` → `(ids, more)`; `threads.list(q=before:<unix_secs>, labelIds=["INBOX"])`; does NOT touch `gmail_last_history_id`; caller owns lock.

## Data & state touched

### Postgres (via own `SessionLocal()`, not `Depends(get_db)`) — §2.3
| Table | R/W |
|-------|-----|
| `User` (`gmail_last_history_id`, OAuth tokens) | R cursor; W cursor via `inbox_repo.update_user_history_id` |
| `InboxThread` (`id`, `gmail_id`, `subject`, `bucket_id`, `recent_message_id`, `is_archived`, `last_activity_at`) | R candidates/rows/`load_parsed_threads`; W upsert + reclassify `bucket_id` + archive/un-archive flips + pointer recompute |
| `InboxMessage` (`from_addr`, `body_preview`, `body_text`, `labels`, `is_unread`, `is_deleted`, `gmail_internal_date`, `gmail_history_id`) | R for candidates/oldest cursor/`load_parsed_threads`; W upsert; soft-delete (`is_deleted=True`) on `messagesDeleted` history records. `clear_user_inbox` (hard delete, msgs→threads FK order) is account-deletion-only and uncalled by any worker path. |
| `Bucket` | R via `bucket_repo.list_active` |
| `LlmCall` | W one row per `call_messages` invocation via `llm/metrics.record_call` (own short session, own commit, swallows all exceptions). |

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
- OpenRouter (OpenAI-compatible `chat.completions`) — §2.10: via `llm_client.call_messages` (AsyncOpenAI, `base_url=OPENROUTER_BASE_URL`) under shared `Semaphore(LLM_CONCURRENCY)` on the worker's LLM loop thread; `classify` (sync paths, `user_id=` passthrough) + `score_thread` (preview). Model is the provider-prefixed `LLM_CLASSIFY_MODEL`. Every call records one `llm_calls` row (success or error) via `llm/metrics.record_call` — see Files.

### Env-var names (from app/config.py — do not read .env)
`REDIS_URL`, `DATABASE_URL`, `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL` (default `https://openrouter.ai/api/v1`), `LLM_CLASSIFY_MODEL` (default `anthropic/claude-haiku-4-5`), `LLM_CONCURRENCY` (default 16). Process env (not config): `CELERY_TASK_ALWAYS_EAGER`.

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
Worker (LLM thread) ──[POST chat/completions, semaphore-bounded]──> openrouter.ai/api/v1  (§2.10)
  Worker (LLM thread) ──[asyncio.to_thread: INSERT llm_calls]──> Postgres (fire-and-forget, per call)
draft_preview_bucket ──[SET preview:{draft_id} EX 600]──> Redis  (BEFORE next line)
Worker ──[PUBLISH user:{uid} {event,...}]──> Redis ──> API PubSubDispatcher ──[SSE]──> Browser  (§2.7)
```
Published events → browser over SSE: `threads_updated {thread_ids}` (poll/full/reclassify), `extend_complete {thread_ids, more}` (extend), `bucket_draft_preview {draft_id, positives, near_misses}` (preview). Browser then re-reads via `/api/threads/batch` (filters by `InboxThread.id` — why workers return internal ids, not gmail ids). Preview also pollable via `GET /api/buckets/draft/preview/{draft_id}` (preview cache fallback). Both `_reclassify_all` and `_score_all` skip the `Gmail v1 threads.get` step entirely — they read `inbox_repo.load_parsed_threads` (Postgres) instead, so reclassify/preview latency is now LLM-bound, not Gmail-API-bound.

## Decision points & gotchas
- **sync_lock serialization**: poll / full / extend / reclassify acquire `sync_lock:{uid}` (NX, 600s TTL) to avoid racing the `(user_id, gmail_id)` unique constraint. Contended → poll/full/extend skip silently; `reclassify_user_inbox` instead `self.retry(countdown=30)`. `draft_preview_bucket` does NOT hold it across the task — only `_extend_inline` grabs it transiently (skips if held).
- **active-user gating**: only uids in the `active_users` zset (added by `/sse` on first connect, TTL 60s, refreshed) get polled. `enqueue_polls` purges expired before listing — disconnected users stop being polled.
- **ack semantics**: `task_acks_late=False` (ack on receipt, not completion) — periodic poll is best-effort; a worker crash mid-task loses that run (next beat tick re-polls).
- **cache-before-publish ordering**: `draft_preview_bucket` calls `preview_cache.store_result` BEFORE `_publish` so a client polling between the two reads the ready result, not stale "pending". `mark_pending` is written by the API at enqueue time so the GET never 404s a fast poller.
- **full_sync_inbox is reconciling, not destructive**: it re-lists the latest 200 `labelIds=["INBOX"]` threads and upserts (archives/un-archives based on presence in that listing) — it never deletes rows. The old wipe-then-repopulate approach was removed because Phase 2 task tables FK onto `inbox_threads.id` and `HistoryGoneError` recovery (which calls this fn) must not be destructive. `clear_user_inbox` (hard delete) still exists in `inbox_repo` but is account-deletion-only and has zero call sites in the sync/worker path.
- **HistoryGoneError / cursor expiry**: `fetch_history_records` 404 (cursor older than ~30-day window) → fall back to `full_sync_inbox` (reconciling upsert, see above — NOT a wipe). Same fallback in `poll_new_messages` and `_inline_reload`. `extend_inbox_history` deliberately leaves cursor untouched (must stay anchored at newest message).
- **widened historyTypes**: partial sync now also mirrors Gmail-side archive (`labelsAdded`/`labelsRemoved` INBOX), soft-delete (`messagesDeleted` → `is_deleted` + pointer recompute + archive-when-empty), and read state (`UNREAD` label → `InboxMessage.is_unread`) — not just new messages. A thread with INBOX freshly added that this worker has never stored is ingested on the spot rather than dropped.
- **reclassify/score paths are Postgres-only, no Gmail refetch**: `_reclassify_all` and `_score_all` both call `inbox_repo.load_parsed_threads` to rebuild `ParsedThread`s from stored `body_text`/`labels` rows instead of looping `gmail.threads().get()` per candidate — a 200-thread reclassify is now bound by LLM concurrency (`LLM_CONCURRENCY`), not by 200 sequential Gmail API round-trips.
- **noise filter**: empty history records → `poll_new_messages` returns without publishing (no `subscribers=0` churn).
- **score thresholds (preview)**: positives `score >= 7` (top 3), near-misses `4 <= score <= 6` (top 3).
- **INBOX scoping**: `labelId="INBOX"` (singular, history.list) / `labelIds=["INBOX"]` (plural, threads.list) — without it SENT/DRAFTS leak into the inbox table.
- **beat single-replica**: beat service `numReplicas=1` (multiple beats multiply Gmail fan-out). Beat owns no app code beyond `beat_schedule.py`.
- **eager mode**: `CELERY_TASK_ALWAYS_EAGER=1` (tests) runs tasks synchronously in-process with `task_eager_propagates=True`; `SessionLocal` is module-rebindable for in-memory engines.
- **alembic NOT run by worker/beat**: Dockerfile chains `alembic upgrade head` before uvicorn, but worker/beat override CMD — only the API container migrates. Migration `0006_data_floor` dialect-guards its FTS DDL (generated tsvector columns, GIN indexes, `pg_trgm`) behind a Postgres check so it still applies cleanly against the SQLite test engine (which just skips that block).
- **fixed countdown=0**: `enqueue_polls` enqueues with `countdown=0` for test determinism (the docstring notes prod could randomize a 0–10s spread).
- **llm_calls is fire-and-forget metrics, not a source of truth for control flow**: `record_call` swallows every exception and never raises — a Postgres outage degrades to "no metrics row," never to a failed classify/score/preview call. Success vs. error rows are distinguished by content-extraction succeeding (`resp.choices[0].message.content`) BEFORE the metrics call — a malformed 200 response records as `outcome="error"`, not a phantom success.
