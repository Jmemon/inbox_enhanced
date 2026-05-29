<!-- stamp: 13a07e5 (main) | 2026-05-29 -->

# Inbox Sync Index

> Scope: three-way inbox synchronization — Gmail ↔ Postgres (`inbox_threads`/`inbox_messages`) ↔ browser. Full/partial/extend sync engine, the Gmail history cursor (`gmail_last_history_id`) + `HistoryGoneError`, the Celery poll/full/extend/reclassify tasks, the `sync_lock`/`active_users` Redis gates, the `user:{uid}` pubsub→SSE push path, and the client `idLayer`/`displayLayer` LWW merge + auto-extend.

This is a cross-cutting doc: it threads the Worker, Data, Realtime, API, and Client
subsystems along the single axis of "keep the inbox in sync." For depth on any one
layer see its dedicated index (most not yet authored — fall back to `ARCHITECTURE.md`).

## Files

| Path | Role / key exports |
|------|--------------------|
| `server/app/workers/gmail_sync.py` | Sync engine (no Celery deps). `fetch_history_records(gmail, *, start_history_id) -> (records, latest_id)` (raises `HistoryGoneError` on 404). `partial_sync_inbox(db, *, user, history_records=None, new_history_id=None) -> list[internal_id]`. `full_sync_inbox(db, *, user) -> list[internal_id]`. `extend_inbox_history(db, *, user, before_internal_date_ms) -> (ids, more)`. Privates `_classify_batch` (loads active buckets + existing `bucket_id` as stability hint), `_upsert_thread_with_messages`. `class HistoryGoneError`. |
| `server/app/workers/tasks.py` | Celery tasks. `enqueue_polls` (beat fan-out). `poll_new_messages(uid)`. `full_sync_inbox_task(uid)`. `extend_inbox_history_task(uid, before_ms)`. `reclassify_user_inbox(self, uid)` (`bind=True, max_retries=3`). `draft_preview_bucket(...)`. Publish helpers `_publish(uid, event, payload)` (logs redis subscriber count), `_publish_thread_ids`. Reclassify privates `_inline_reload`, `_reclassify_all`. `SessionLocal` is a module attr (test monkeypatch seam). |
| `server/app/workers/beat_schedule.py` | `beat_schedule` dict — single entry `enqueue-polls-every-30s` → `app.workers.tasks.enqueue_polls`, `schedule(run_every=30.0)`. Beat MUST be 1 replica (`railway.beat.toml`). |
| `server/app/inbox/inbox_repo.py` | Postgres read/write layer. NEVER commits (caller owns txn). `upsert_thread`, `upsert_message` (recomputes `recent_message_id` via indexed `ORDER BY gmail_internal_date DESC LIMIT 1`), `list_threads` (recent-msg date desc, nulls last), `get_thread`, `get_threads_batch` (silently drops foreign/unknown ids; order NOT preserved), `get_message`, `update_user_history_id`, `clear_user_inbox` (messages→threads, FK order). |
| `server/app/gmail/parser.py` | `ParsedMessage`, `ParsedThread`, `parse_message`, `assemble_thread(*, thread_id, raw_messages)` (sorts msgs by internalDate; subject from earliest, `recent_internal_date` from latest), `thread_to_string` (uses full `body_text`, not the 150-char `body_preview`). |
| `server/app/gmail/client.py` | `get_gmail_client(db, user)` — builds `gmail v1` client, refreshes/decrypts token. (See AUTH for refresh internals.) |
| `server/app/db/models.py` | `InboxThread` (uq `(user_id, gmail_id)` = `uq_inbox_threads_user_gmail`; `recent_message_id` soft pointer, no FK; `bucket_id` FK). `InboxMessage` (uq `(user_id, gmail_id)`; `gmail_internal_date` BigInteger **indexed**; `gmail_history_id`). `User.gmail_last_history_id String(64)`. |
| `server/app/realtime/sync_lock.py` | `acquire(uid, *, ttl_seconds=600) -> bool` (`SET sync_lock:{uid} 1 NX EX`), `release(uid)`. Per-user mutex around any writer. |
| `server/app/realtime/active_users.py` | zset `active_users`, score = expiry unix-secs. `add/refresh(uid, *, ttl_seconds)`, `remove`, `list_active()` (`ZRANGE 0 -1`), `purge_expired()` (`ZREMRANGEBYSCORE -inf now`). |
| `server/app/realtime/pubsub.py` | `PubSubDispatcher` (one per uvicorn worker). `subscribe/unsubscribe(uid)` on `user:{uid}`; `_run` drains `pubsub.listen()`, routes frames into per-tab queues via `put_nowait` (drops on `QueueFull`). `_has_subscription` event gates network until first SSE. Module fns `start/stop/subscribe/unsubscribe`. |
| `server/app/realtime/sse_connections.py` | Per-process `dict[uid -> set[asyncio.Queue]]`. `add -> is_first`, `remove -> was_last`, `iter_queues`, `has_local`, `reset` (test hook). Per-process only — each uvicorn worker subscribes redis for its own queues. |
| `server/app/api/sse.py` | `GET /api/sse` (cookie-auth, no path uid). Registers queue, subscribes pubsub + `active_users.add` on `is_first`, fires kickoff task, streams `data:`/`: keepalive` frames; on last disconnect unsubscribes + `active_users.remove`. Consts: `QUEUE_MAXSIZE=100`, `HEARTBEAT_SECONDS=5`, `ACTIVE_USERS_TTL_SECONDS=60`. |
| `server/app/api/inbox.py` | `GET /api/inbox`, `GET /api/threads/{id}`, `POST /api/threads/batch`, `POST /api/inbox/extend`, `POST /api/inbox/refresh`. `_serialize_thread` (user-scoped recent msg). Consts `DEFAULT_LIMIT=50`, `MAX_LIMIT=200`, `MAX_BATCH_IDS=500`. |
| `client/src/lib/sse.ts` | `EventSource('/api/sse')` singleton + `subscribeSse(handler)`. Emits synthetic `_open`/`_error` plus parsed data events. Auto-reconnects via `queueMicrotask(_open)` while handlers remain. Types: `SseDataEvent` = `threads_updated` \| `bucket_draft_preview` \| `extend_complete`. |
| `client/src/pages/inbox/useInbox.tsx` | `idLayer` (ordered ids) + `displayLayer` (id→thread) model. `SNAPSHOT_LIMIT=200`, `PAGE_SIZE=50`, `EXTEND_TIMEOUT_MS=90_000`. `fetchAndReplace`/`snapshot`/`resync`, `applyThreadUpdates` (LWW gate), `requestExtend`, auto-extend `useEffect`, extend watchdog. |
| `client/src/pages/inbox/useInboxSse.tsx` | Buffers `threads_updated` until `snapshot()` completes after each `_open`; replays buffer then streams live; `_error` resets the ready flag. |

## Routes / Tasks / Entrypoints

**HTTP (API process):**
- `GET /api/sse` — browser opens one per tab. First conn for a user → pubsub subscribe + `active_users.add` + kickoff sync task. Streams JSON `data:` frames + 5s keepalive (each keepalive also refreshes the `active_users` TTL).
- `GET /api/inbox?limit=&page=` — paginated list, newest-active first; returns `{as_of, page, limit, threads}`. `limit` clamps 1–200, default 50; client fetches 200.
- `GET /api/threads/{id}` — single thread (rare fallback); 404 if not owned.
- `POST /api/threads/batch {thread_ids}` — SSE-replay bulk fetch; ≤500 ids; foreign/unknown silently dropped.
- `POST /api/inbox/refresh` — 202; Reload button. Branches on cursor: present → `poll_new_messages`, absent → `full_sync_inbox_task`. Result arrives via SSE.
- `POST /api/inbox/extend {before_internal_date}` — 202; enqueues `extend_inbox_history_task`. Result via SSE `extend_complete`.

**Celery tasks (worker process):**
- `enqueue_polls` — beat-fired (30s). `purge_expired()` → `list_active()` → `poll_new_messages.apply_async([uid], countdown=0)` per active uid.
- `poll_new_messages(uid)` — `sync_lock.acquire` (silent return if held). No cursor → `full_sync_inbox`. Else `fetch_history_records`: 404 → `full_sync_inbox`; empty → silent (no publish); records → `partial_sync_inbox(records, new_id)`. Publishes `threads_updated` with internal ids.
- `full_sync_inbox_task(uid)` — locked full sync; SSE kickoff (no cursor) + refresh (no cursor).
- `extend_inbox_history_task(uid, before_ms)` — locked; `extend_inbox_history`; publishes `extend_complete {thread_ids, more}`.
- `reclassify_user_inbox(uid)` — locked (retry `countdown=30` if contended, max 3). `_inline_reload` (poll-equivalent without re-locking) + `_reclassify_all` (refetch all, classify w/ stability hint, write only changed `bucket_id`). Publishes union of touched ids. Triggered by `POST /api/buckets`.

**Beat:** `enqueue-polls-every-30s` → `enqueue_polls`. One replica only.

**Client hooks:** `useInboxSse` (snapshot+buffer lifecycle) drives `useInbox.applyThreadUpdates`; `useInbox` owns pagination + auto-extend.

## Data & state touched

**Postgres (system of record):**
- `inbox_threads` — write: thread upserts (full/partial/extend/reclassify), `bucket_id` reassignment (reclassify), `recent_message_id` pointer; delete: `clear_user_inbox` (full sync). Read: `list_threads`, `get_thread(s)`. Uq `(user_id, gmail_id)`.
- `inbox_messages` — write: message upserts; delete on full sync. `gmail_internal_date` (indexed) is the sort/LWW key; `gmail_history_id` feeds cursor advancement. Uq `(user_id, gmail_id)`.
- `users.gmail_last_history_id` — write: `update_user_history_id` (partial = `historyId` from response; full = max history_id across ingested msgs). Read: `poll_new_messages`, `/api/inbox/refresh`, SSE kickoff to choose full vs partial. **Extend does NOT touch it.**

**Redis (3 roles, one instance):**
- `sync_lock:{uid}` — `SET NX EX 600`. Held by every writer task; releases in `finally`. TTL is the crash safety net.
- `active_users` (zset) — `ZADD` on first SSE conn + every 5s heartbeat (score = now+60); `ZREM` on last disconnect; `enqueue_polls` reads + purges. Gates *whom beat polls* (only users with a live SSE).
- `user:{uid}` (pubsub) — worker `PUBLISH`es; per-process dispatcher subscribes/routes to SSE queues. Transient; dropped if no subscriber.
- (broker/backend) — task messages.

**External:** Gmail v1 (`history.list` `historyTypes=[messageAdded]` `labelId="INBOX"`; `threads.list maxResults=200 labelIds=["INBOX"]` ±`q=before:<secs>`; `threads.get format=full`); Anthropic (classify, per-thread, via `_classify_batch` — see LLM).

## Data flows / cross-subsystem touchpoints

Periodic poll (the steady-state loop):
```
Beat ──[30s: enqueue_polls]──> Worker
  Worker ──[ZRANGE active_users]──> Redis
  Worker ──[apply_async poll_new_messages(uid)]──> Worker
    Worker ──[history.list startHistoryId=cursor]──> Gmail v1
    Worker ──[threads.get format=full (per touched tid)]──> Gmail v1
    Worker ──[classify]──> Anthropic
    Worker ──[upsert threads+msgs, advance cursor, COMMIT]──> Postgres
    Worker ──[PUBLISH user:{uid} {threads_updated, thread_ids:[internal ids]}]──> Redis
      Redis ──[message frame]──> API PubSubDispatcher ──[put_nowait]──> SSE queue
        API ──[data: {...}\n\n]──> Browser EventSource
          useInboxSse ──[onApply ids]──> useInbox.applyThreadUpdates
            Browser ──[POST /api/threads/batch {thread_ids}]──> API
              API ──[get_threads_batch]──> Postgres ──[{threads}]──> Browser (LWW merge into displayLayer)
```

Connect / kickoff:
```
Browser ──[GET /api/sse]──> API
  API: sse_connections.add → is_first? subscribe(user:{uid}) + active_users.add
  API ──[apply_async: cursor? poll_new_messages : full_sync_inbox_task]──> Worker  ──(same publish→SSE path)
useInboxSse: on _open → snapshot() [GET /api/inbox limit=200] → flush buffered threads_updated → live
```

Reload: `Browser ──[POST /api/inbox/refresh]──> API ──[apply_async poll/full]──> Worker` → SSE (same as poll). Client `resync()` also re-pulls `/api/inbox` without blanking the list.

Extend (paginate into history):
```
useInbox auto-extend (page ≥ pageCount-1) ──[POST /api/inbox/extend {before_internal_date=smallest}]──> API
  API ──[apply_async extend_inbox_history_task]──> Worker
    Worker ──[threads.list q=before:<secs>]──> Gmail v1 → classify → upsert (cursor untouched)
    Worker ──[PUBLISH {extend_complete, thread_ids, more}]──> Redis → SSE → useInbox (setMore + applyThreadUpdates)
```

Reclassify (bucket created): `POST /api/buckets ──> reclassify_user_inbox` → inline reload + reclassify-all → publish union as `threads_updated`. Client also runs a 60s+150s watchdog `resync()` (see `Home.tsx`) to cover SSE loss during the long (~110s) task.

## Decision points & gotchas

- **Cursor expiry → full sync.** `history.list` 404 (`startHistoryId` past Gmail's ~30-day window) → `HistoryGoneError` → `full_sync_inbox` (wipe + repopulate latest 200). No cursor at all → also full sync. Same branch in `poll_new_messages`, `/api/inbox/refresh`, SSE kickoff, and `_inline_reload`.
- **Empty history = no publish.** `poll_new_messages` returns silently on 0 records — do NOT publish on noise, or every 30s tick wakes idle clients.
- **`sync_lock` prevents half-synced inboxes.** Concurrent SSE-kickoff + beat poll would both take the full-sync path and collide on uq `(user_id, gmail_id)`, rolling back the txn → empty inbox. All writers (`poll`, `full`, `extend`, `reclassify`, draft-preview inline-extend) acquire it; non-reclassify tasks return silently if held, reclassify retries (30s, ×3).
- **Cursor anchoring.** Partial sync advances the cursor to the response `historyId`; full sync to the max `gmail_history_id` across ingested messages; **extend must NOT touch the cursor** (it pulls *older* mail — moving the cursor back would make the next partial sync re-ingest or miss).
- **Per-thread `threads.get` is try/except in partial+extend.** A thread deleted between the history record and the fetch 404s; without the guard the whole task crashes, the cursor never advances, and the 30s beat retries forever on the same dead thread. Full sync does NOT guard (a stub from `threads.list` should still exist).
- **Internal id, not gmail id, on the wire.** Sync fns return `InboxThread.id` (UUID hex). `threads_updated`/`extend_complete` carry these because `/api/threads/batch` filters by `InboxThread.id` — publishing `gmail_thread_id` would return zero rows.
- **Publish-after-commit; cache-before-publish (preview).** Sync commits to Postgres *before* `PUBLISH`, so the `batch` replay always finds the rows. (Draft-preview likewise `store_result` before publishing — see BUCKETS.)
- **`INBOX` label scoping.** `history.list` uses singular `labelId="INBOX"`; `threads.list` uses plural `labelIds=["INBOX"]`. Without these, SENT/DRAFTS leak in and your own sent mail surfaces as inbox threads "from another address."
- **LWW merge on the client.** `applyThreadUpdates` accepts an incoming thread only if `recent_message.internal_date >= stored` (`lastInternalDate` ref), then re-sorts `idLayer` by that date desc. `fetchAndReplace` *resets* (not merges) the gate map so a thread that fell out of the latest-200 window can't keep a stale value.
- **Active-user gating.** Beat only polls users in `active_users`; the SSE heartbeat (5s) refreshes a 60s TTL, and `enqueue_polls` purges expired entries — so a dead API process can't strand a user as "active." (Note: `active_users.refresh` docstring says "every 20s" but the live cadence is the SSE `HEARTBEAT_SECONDS=5`.)
- **SSE delivery is best-effort.** Dispatcher `put_nowait` drops frames on a full queue (`QUEUE_MAXSIZE=100`), and `_publish` logs `subscribers=0` when nobody's listening (subscribe/unsubscribe churn during SSE flapping). The client compensates with the 90s extend watchdog, the reclassify resync timers, and the `snapshot`-then-buffer-flush in `useInboxSse`.
- **Eager-mode tests.** `CELERY_TASK_ALWAYS_EAGER=1` runs tasks inline inside the test txn; `fakeredis` backs `sync_lock`/`active_users`/pubsub; `SessionLocal` in `tasks.py` is monkeypatched onto the in-memory engine.
