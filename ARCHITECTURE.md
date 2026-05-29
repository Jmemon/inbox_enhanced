# Architecture

> Stamped at commit `2cff277` on branch `main`.

A first-principles map of `inbox_enhanced`: what code is running on what compute, when it runs, and what made it run. The five sections separate the compute/runtime layer from the code layer so the system can be re-architected without re-reading the codebase.

---

## 1. Processes (compute)

Each entry is one process (or per-process subprocess/task) with its trigger. "Process" here means an OS process or a long-lived asyncio task that owns its own scheduling — short-lived request handlers and individual celery tasks are listed under their host process.

### 1.1 API process — FastAPI under uvicorn

- **Where**: One Railway service (`railway.toml`), built from `Dockerfile`. Container CMD:
  ```
  uv run alembic upgrade head && \
  uv run uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'
  ```
- **What runs**: `app.main:app` (FastAPI). Health-gated by Railway on `/api/health`.
- **Trigger**: Container start (Railway deploy or restart). Then per-request triggers below.
- **Long-lived sub-tasks inside this process**:
  - **`alembic upgrade head`** — boot-time. Trigger: container CMD shell preamble. One-shot per process start.
  - **PubSub dispatcher task** (`app.realtime.pubsub.PubSubDispatcher._run`). Trigger: FastAPI `lifespan` hook in `app/main.py`. One per uvicorn worker process. Idle-wait on `_has_subscription` until first SSE `subscribe()`, then `async for raw in pubsub.listen()` forever. Routes redis pubsub frames into per-tab in-memory queues.
  - **SSE `event_stream` coroutines** (`app.api.sse.sse`). Trigger: each inbound `GET /api/sse` opens one. Lifecycle = lifetime of the streaming HTTP response. Each polls its `asyncio.Queue` with a 1s timeout, emits a `: keepalive\n\n` frame every 5s (`HEARTBEAT_SECONDS`), and on heartbeat refreshes the user's `active_users` TTL in redis.
- **Short-lived units (one per HTTP request)**:
  - `auth_router` — `/auth/login`, `/auth/callback`, `/auth/me`, `/auth/logout`. Trigger: inbound HTTP from browser.
  - `buckets_router` — `/api/buckets` GET/POST/PATCH/DELETE, `/api/buckets/draft/preview` POST/GET.
  - `inbox_router` — `/api/inbox`, `/api/threads/{id}`, `/api/threads/batch`, `/api/inbox/refresh`, `/api/inbox/extend`.
  - `gmail_router` — `/api/gmail/profile`.
  - SPA catch-all (`spa_catch_all`) — serves `app/static/index.html` for non-API paths.
  - StaticFiles mount at `/assets` — serves vite-built hashed bundles.

### 1.2 Celery worker process

- **Where**: Separate Railway service (`railway.worker.toml`). Same Docker image as the API; CMD is overridden:
  ```
  uv run celery -A app.workers.celery_app worker --loglevel=info --concurrency=4
  ```
- **What runs**: A celery prefork worker pool with 4 children that consume tasks from `redis://...` (broker == backend == REDIS_URL).
- **Trigger**: Long-lived daemon. Each task execution is triggered by a redis broker message produced by either beat or the API process via `apply_async`.
- **Tasks executed** (all in `app/workers/tasks.py`):
  - `enqueue_polls` — fan-out: purge expired `active_users` zset entries, then `poll_new_messages.apply_async` per active uid.
  - `poll_new_messages(user_id)` — lock → if no `gmail_last_history_id`, full sync; else `users.history.list`. Falls back to full sync on 404 (cursor expired). Publishes `threads_updated` on the redis pubsub channel `user:{user_id}` with internal thread ids.
  - `full_sync_inbox_task(user_id)` — wipe + repopulate from latest 200 threads. Used by SSE-on-connect kickoff and `/api/inbox/refresh` for users without a cursor.
  - `extend_inbox_history_task(user_id, before_internal_date_ms)` — pull older threads (q=before:<unix_secs>). Publishes `extend_complete`.
  - `draft_preview_bucket(user_id, draft_id, name, description, exclude_thread_ids)` — score up to 100 inbox candidates against a prospective bucket via Anthropic; cache result, publish `bucket_draft_preview`.
  - `reclassify_user_inbox(user_id)` — bound, max_retries=3. Inline reload + reclassify all threads. Retries w/ 30s countdown if `sync_lock` held.
- **Long-lived sub-tasks inside this process** (lazy):
  - **LLM event loop thread** (`app.llm.client._ensure_initialized`). Trigger: first call to `client.run_in_loop` or `call_messages`. One daemon thread per worker process running `loop.run_forever()`. Hosts a single `AsyncAnthropic` client and an `asyncio.Semaphore(ANTHROPIC_CONCURRENCY=16)` shared across classify + score paths.
  - **`alembic upgrade head`** still runs at container start (the `Dockerfile` CMD chains it before uvicorn, but the worker overrides CMD with the celery command, so alembic is NOT run by worker boot — only the API container migrates).

### 1.3 Celery beat process

- **Where**: Separate Railway service (`railway.beat.toml`). `numReplicas = 1` enforced (multiple beats would multiply Gmail fan-out). CMD:
  ```
  uv run celery -A app.workers.celery_app beat --loglevel=info --schedule=/tmp/celerybeat-schedule
  ```
- **What runs**: `celery beat` scheduler reading `app.workers.beat_schedule.beat_schedule`.
- **Trigger**: Container start. Internal 30-second tick fires `enqueue-polls-every-30s` indefinitely.
- **Effect**: Pushes one `enqueue_polls` task message to redis every 30s.

### 1.4 Postgres

- **Where**: Local: `postgres:16` container in `docker-compose.yml`. Production: Railway managed Postgres referenced via `DATABASE_URL`.
- **Trigger**: TCP from API + worker via `psycopg`/SQLAlchemy. SQLAlchemy normalises `postgresql://` → `postgresql+psycopg://` in `app/db/session.py`.
- **What's stored**: see Section 3 (DAG) and the table list below in the storage section.

### 1.5 Redis

- **Where**: Local: `redis:7-alpine` container. Production: Railway managed Redis via `REDIS_URL`.
- **Trigger**: TCP from API, worker, beat.
- **Triple role** ("three roles, one instance"):
  1. Celery broker + result backend.
  2. Pubsub channels `user:{user_id}` (transient frames).
  3. Key/value store: `active_users` (sorted set), `sync_lock:{user_id}` (NX EX), `preview:{draft_id}` (cached preview blobs).

### 1.6 Browser SPA process

- **Where**: User's browser tab. Bundle is built from `client/` by Vite into `server/app/static/` and served by FastAPI's `StaticFiles` + SPA catch-all.
- **Trigger**: User loads the page → browser executes the bundle.
- **Long-lived in-tab "processes"**:
  - **EventSource singleton** (`client/src/lib/sse.ts`). Trigger: first `subscribeSse(handler)` call. One `EventSource('/api/sse', { withCredentials: true })` per tab; auto-reconnects (`queueMicrotask(_open)`) on error if any handlers remain.
  - **Auth context bootstrap** (`AuthProvider` in `useAuth.tsx`). Trigger: `App` mount → `useEffect` hits `/auth/me`.
  - **Polling fallback for draft preview** (`NewBucketModal`). Trigger: enter "pending" step. Polls `GET /api/buckets/draft/preview/{draft_id}` every 5s while pending.
  - **Reclassify watchdog** (`Home.tsx`). Trigger: bucket creation success. Two `setTimeout`s at 60s + 150s call `inbox.resync()` to compensate for SSE delivery loss during `reclassify_user_inbox`.
  - **Extend watchdog** (`useInbox.tsx`). Trigger: `requestExtend` kickoff. `setTimeout(EXTEND_TIMEOUT_MS=90_000)` resets the in-flight flag if `extend_complete` doesn't arrive.
  - **Auto-extend trigger** (`useInbox.tsx`). Trigger: `useEffect` watching `page` / `pageCount`. Fires `requestExtend` when user reaches the page-before-last and there's no in-flight extend.
- **Short-lived units**: per-render React function components and per-event handlers.

### 1.7 Build-time processes

- **Vite/Bun** (`Dockerfile` stage 1). Trigger: container build (Railway deploy or local `docker build`). Runs `bun install --frozen-lockfile` then `bun run build` producing the SPA bundle. Final step greps the output for secret-shaped strings and fails the build if any are found.
- **uv sync** (`Dockerfile` stage 2). Trigger: container build. Installs Python deps from `pyproject.toml` + `uv.lock`.
- **Local helper scripts** (`scripts/`). Trigger: developer manual invocation.

### 1.8 Test processes

- **pytest** (`server/tests`). Trigger: developer manual invocation. `CELERY_TASK_ALWAYS_EAGER=1` env makes celery tasks run synchronously inside the test transaction.
- **fakeredis** dev dependency replaces redis for unit tests.

---

## 2. Inter-process data flow (edges)

Notation: `A ──[channel: data] ──> B`. Direction is publisher → consumer.

### 2.1 Browser ↔ API

- `Browser ──[HTTPS GET /auth/login]──> API`. API redirects (302) to Google.
- `Google ──[HTTP 302 with ?code&state]──> Browser ──[HTTPS GET /auth/callback]──> API`. API exchanges the code, encrypts tokens, persists to `users`, mints session, sets `Set-Cookie: session=<sid>`.
- `Browser ──[HTTPS, Cookie: session]──> API`: every `/api/*` and `/auth/*` request. `app.deps.get_current_user` resolves it via `sessions.lookup_active_session`.
- `Browser ──[HTTP GET /api/sse, text/event-stream]──> API`. Server-Sent Events: long-lived response. API → Browser frames are JSON-encoded `data:` payloads (`threads_updated`, `bucket_draft_preview`, `extend_complete`) plus `: keepalive` heartbeats every 5s.
- `Browser ──[HTTPS POST /api/threads/batch, body: {thread_ids:[…]}]──> API ──[JSON {threads:[…]}]──> Browser`. SSE replay path.
- `Browser ──[HTTPS POST /api/inbox/refresh|/api/inbox/extend]──> API`. 202 returned; result delivered later via SSE.
- `Browser ──[HTTPS POST /api/buckets/draft/preview]──> API ──[JSON {draft_id}]──> Browser`. Result delivered via SSE OR polled `GET /api/buckets/draft/preview/{draft_id}`.

### 2.2 API ↔ Postgres

- Bi-directional SQL over TCP via SQLAlchemy + psycopg. Read-modify-write within FastAPI request scope. Tables: `users`, `sessions`, `buckets`, `inbox_threads`, `inbox_messages`.
- API writes: session lifecycle (insert/revoke), bucket CRUD, user upsert at OAuth callback, encrypted token rotation when `ensure_fresh_access_token` refreshes.

### 2.3 Worker ↔ Postgres

- Bi-directional SQL over TCP. Worker uses its own `SessionLocal` per task (no FastAPI request scope). Writes: thread + message upserts (subject to `(user_id, gmail_id)` unique constraints), `users.gmail_last_history_id` cursor advancement, `users.bucket_id` reassignment after reclassify.

### 2.4 API → Redis

- **Active user TTL refresh**: `ZADD active_users {user_id: now+60}` from the SSE heartbeat path (every 5s).
- **Add active user**: `ZADD` on first SSE connection.
- **Remove active user**: `ZREM` when the last SSE connection for a user closes.
- **Pubsub subscribe/unsubscribe**: `SUBSCRIBE user:{user_id}` from the dispatcher in the API process when first SSE connection opens; `UNSUBSCRIBE` when last closes.
- **Pubsub message inbound**: `Worker ──[PUBLISH user:{user_id}, json]──> Redis ──[message frame]──> API`. The dispatcher routes to local `asyncio.Queue`s registered by SSE handlers.
- **Sync_lock check** (read-only at API surface): not currently — only the worker writes; the API never touches `sync_lock:*`.
- **Preview cache write** (mark_pending): `SET preview:{draft_id} {status:pending,...} EX 600` from `POST /api/buckets/draft/preview` BEFORE the celery enqueue (avoids a 404 race).
- **Preview cache read**: `GET preview:{draft_id}` from `GET /api/buckets/draft/preview/{draft_id}`.

### 2.5 API → Celery broker (Redis) → Worker

- `API ──[apply_async(...) → LPUSH celery queue]──> Redis ──[BRPOP]──> Worker`.
- Tasks enqueued from the API:
  - `poll_new_messages(uid)` from `/api/sse` (kickoff if cursor exists), `/api/inbox/refresh`.
  - `full_sync_inbox_task(uid)` from `/api/sse` (kickoff if no cursor), `/api/inbox/refresh` (no cursor branch).
  - `draft_preview_bucket(uid, draft_id, name, description, excludes)` from `POST /api/buckets/draft/preview`.
  - `extend_inbox_history_task(uid, before_ms)` from `POST /api/inbox/extend`.
  - `reclassify_user_inbox(uid)` from `POST /api/buckets`.

### 2.6 Beat → Celery broker → Worker

- `Beat ──[every 30s: LPUSH enqueue_polls]──> Redis ──[BRPOP]──> Worker`. Worker's `enqueue_polls` then:
- `Worker ──[apply_async per uid]──> Redis ──[BRPOP]──> Worker` (could be a different worker process / replica).

### 2.7 Worker → Redis (state)

- `SET sync_lock:{uid} 1 NX EX 600` to acquire per-user sync lock; `DEL` to release.
- `ZRANGE active_users 0 -1` and `ZREMRANGEBYSCORE` from `enqueue_polls`.
- `SET preview:{draft_id} {status:ready,...} EX 600` from `draft_preview_bucket` before publishing.
- `PUBLISH user:{uid} {event,...}` to fan messages out to API processes for SSE delivery.

### 2.8 API ↔ Google

- OAuth: `API ──[HTTPS POST oauth2.googleapis.com/token]──> Google` for code exchange + refresh. `oauth2 v2 userinfo` for email/name.
- `/api/gmail/profile`: `API ──[HTTPS users.getProfile + users.messages.list/get]──> Gmail v1`.

### 2.9 Worker ↔ Google

- All sync paths: `Worker ──[users.history.list / threads.list / threads.get / token refresh]──> Gmail v1`. Refresh tokens stored encrypted in postgres are decrypted by `app.auth.crypto.Fernet` per call.

### 2.10 Worker ↔ Anthropic

- `Worker (LLM loop thread) ──[HTTPS POST messages]──> api.anthropic.com`. Concurrency capped by per-process semaphore (`ANTHROPIC_CONCURRENCY=16`). Used by classify + score-thread paths.

### 2.11 Browser ↔ Browser (intra-tab)

- `useInboxSse` ↔ `useInbox` via React state + a `Set` of subscribers in `lib/sse.ts`. Multiple hooks subscribe to the same `EventSource`.
- `NewBucketModal` ↔ same SSE bus for `bucket_draft_preview` events.

---

## 3. Code DAGs by process

Each subsection: entrypoint → key call sites → key decision points. Not exhaustive — depth stops at the level needed to relocate code.

### 3.1 API process — request handlers

```
uvicorn ─► app.main:app (FastAPI)
            │
            ├─ lifespan() ─► app.realtime.pubsub.start()
            │                    └─► PubSubDispatcher._run (asyncio task)
            │                         └─ awaits _has_subscription, then listen()
            │
            ├─ /auth/login   ─► google_oauth.build_authorize_url + state_cookie.make_state
            ├─ /auth/callback ─► google_oauth.exchange_code → crypto.encrypt → User upsert
            │                    └─► sessions.create_session → Set-Cookie session=
            ├─ /auth/me      ─► deps.get_current_user
            ├─ /auth/logout  ─► sessions.revoke_session
            │
            ├─ /api/health   ─► returns {status: ok, env}
            │
            ├─ /api/buckets        (GET)    ─► bucket_repo.list_active
            ├─ /api/buckets        (POST)   ─► bucket_repo.formulate_criteria
            │                                 → bucket_repo.create_custom
            │                                 → tasks.reclassify_user_inbox.apply_async  ◄── decision: side-effect
            ├─ /api/buckets/{id}   (PATCH)  ─► _load_owned_or_403 → bucket_repo.rename
            ├─ /api/buckets/{id}   (DELETE) ─► soft delete; idempotent on already-deleted
            ├─ /api/buckets/draft/preview (POST) ─► preview_cache.mark_pending
            │                                       → tasks.draft_preview_bucket.apply_async
            ├─ /api/buckets/draft/preview/{draft_id} (GET) ─► preview_cache.load
            │                                                 → 200/202/404/403 dispatch
            │
            ├─ /api/inbox            ─► inbox_repo.list_threads + _serialize_thread
            ├─ /api/threads/{id}     ─► inbox_repo.get_thread (user-scoped)
            ├─ /api/threads/batch    ─► inbox_repo.get_threads_batch (silently drops foreign ids)
            ├─ /api/inbox/refresh    ─► branch on user.gmail_last_history_id:
            │                          cursor present → poll_new_messages.apply_async
            │                          no cursor      → full_sync_inbox_task.apply_async
            ├─ /api/inbox/extend     ─► tasks.extend_inbox_history_task.apply_async
            │
            ├─ /api/gmail/profile    ─► gmail.client.fetch_profile_summary
            │
            ├─ /api/sse              ─► sse_connections.add(user_id, queue) → is_first?
            │                          if first: pubsub.subscribe(user_id), active_users.add
            │                          kickoff: poll_new_messages OR full_sync_inbox_task
            │                          event_stream() loop:
            │                            queue.get(timeout=1.0) | timeout → heartbeat (5s)
            │                            disconnect detection via request.is_disconnected()
            │                          on close: sse_connections.remove → was_last?
            │                                    if last: pubsub.unsubscribe, active_users.remove
            │
            └─ /{full_path:path}     ─► spa_catch_all → /assets/* via StaticFiles, else index.html

deps.get_current_user (used everywhere): Cookie('session') → sessions.lookup_active_session
                                          → User row or HTTPException(401)
```

### 3.2 Celery worker — task DAGs

```
celery worker ──► broker poll ──► task dispatch
                                   │
  enqueue_polls():
    active_users.purge_expired (ZREMRANGEBYSCORE)
    active_users.list_active   (ZRANGE)
    for uid in active: poll_new_messages.apply_async(uid)

  poll_new_messages(uid):
    sync_lock.acquire(uid) ──┐ no → return silent
                              ▼
    SessionLocal()
    user = db.get(User, uid)  ──► not found → return
    if not user.gmail_last_history_id:                          ◄── decision
        full_sync_inbox(db, user) → publish thread_ids → return
    gmail = get_gmail_client(db, user)
    try:
      history, new_id = fetch_history_records(gmail, start=user.gmail_last_history_id)
    except HistoryGoneError (404):                              ◄── decision (cursor expired)
      full_sync_inbox → publish → return
    if not history: return silent (no publish)                  ◄── decision (noise filter)
    partial_sync_inbox(db, user, history, new_id) → publish

  full_sync_inbox_task(uid):
    sync_lock.acquire → SessionLocal
    full_sync_inbox(db, user) → publish

  extend_inbox_history_task(uid, before_ms):
    sync_lock.acquire → SessionLocal
    extend_inbox_history(db, user, before_ms) → (ids, more)
    publish 'extend_complete' {thread_ids, more}

  draft_preview_bucket(uid, draft_id, name, desc, excludes):
    SessionLocal
    candidates = _read_candidates(limit=100)
    if len < 100: _extend_inline (acquires sync_lock)            ◄── decision (pool too small)
    gmail = get_gmail_client
    scored = _score_all(gmail, candidates, name, desc)
       ├─ sequential gmail.threads.get(format=full)               ◄── ~200ms each, unavoidable
       └─ async gather over llm_client.call_messages (semaphore-bounded)
    positives = top-3 score≥7; near = top-3 score 4..6           ◄── thresholds
    preview_cache.store_result (BEFORE publish)                  ◄── ordering matters
    publish 'bucket_draft_preview'

  reclassify_user_inbox(uid):
    sync_lock.acquire ──no──► self.retry(countdown=30)            ◄── decision
    _inline_reload(db, user)  → list of synced ids
       ├─ no cursor                  → full_sync_inbox
       ├─ HistoryGoneError           → full_sync_inbox
       └─ history records present    → partial_sync_inbox
    _reclassify_all(db, user)
       ├─ load all threads
       ├─ gmail.threads.get(format=full) sequentially
       ├─ classify(threads, buckets, current_bucket_ids)         ◄── stability hint
       └─ write back only changed bucket_id rows
    publish union of synced+reclassified ids

gmail_sync (worker-internal helpers):
  fetch_history_records → users.history.list; raises HistoryGoneError on 404
  partial_sync_inbox    → for each touched gmail thread, threads.get
                           _classify_batch (one parallel LLM call per thread)
                           _upsert_thread_with_messages
                           inbox_repo.update_user_history_id
                           db.commit
  full_sync_inbox       → clear_user_inbox → threads.list maxResults=200 →
                           per-stub threads.get → classify → upsert →
                           advance gmail_last_history_id to max history_id seen
  extend_inbox_history  → threads.list q=before:<unix_secs> → per-stub get →
                           classify → upsert; does NOT touch history cursor

llm_client (worker-process singleton):
  _ensure_initialized → spawn daemon thread running asyncio.run_forever()
                       → AsyncAnthropic + Semaphore(ANTHROPIC_CONCURRENCY)
  call_messages(model, system, user) → semaphore-guarded messages.create
  run_in_loop(coro) → asyncio.run_coroutine_threadsafe → result()
```

### 3.3 Celery beat — DAG

```
celery beat ──► reads beat_schedule from celery_app
                └─ {"enqueue-polls-every-30s": {task: app.workers.tasks.enqueue_polls,
                                                schedule: schedule(run_every=30.0)}}
                Every 30s → push enqueue_polls onto broker.
                No app code beyond beat_schedule.py.
```

### 3.4 Browser SPA — component / hook DAG

```
main.tsx ─► <App>
              └─ <AuthProvider> (useAuth.tsx)
                    state: loading | authed | anon
                    on mount: getJSON('/auth/me') → setState
                    │
                    └─ <Routes>
                         loading → <Splash>
                         anon    → <Login>
                         authed  → <Home>
                                     │
                                     ├─ useBuckets() ─► getBuckets() on mount
                                     │                  create/rename/softDelete → refresh()
                                     ├─ useInbox({buckets, filterSelection})
                                     │     state: idLayer, displayLayer, page, more, extendInFlight
                                     │     fetchAndReplace → getInbox(limit=200)
                                     │     applyThreadUpdates(ids) → getThreadsBatch
                                     │           └─ LWW gate by recent_message.internal_date
                                     │     auto-extend useEffect: page≥pageCount-1 → requestExtend
                                     │           └─ postInboxExtend(smallest_internal_date)
                                     │           └─ 90s watchdog setTimeout
                                     │     subscribeSse('extend_complete') → setMore + apply
                                     │     hydrateCurrentPage on page change
                                     ├─ useInboxSse({onApply, snapshot})
                                     │     subscribeSse → on _open call snapshot, then drain buffer
                                     │     buffer 'threads_updated' until snapshot ready
                                     │     on _error reset ready flag
                                     ├─ <SecondaryHeader>
                                     │     ReloadButton → requestRefresh → resync
                                     │     <FilterByBucketDropdown>
                                     │     <Pagination>
                                     ├─ <InboxList threads={pageThreads} bucketsById={byId} />
                                     ├─ <ViewBucketsModal> (rename, soft-delete)
                                     └─ <NewBucketModal>
                                           steps: form | pending | review
                                           form submit → postBucketDraftPreview → setDraftId
                                           pending → subscribeSse('bucket_draft_preview') OR
                                                     setInterval(5s) getBucketDraftPreview
                                                     idempotent applyPreview (appliedRef)
                                           review submit → onSave (createWithWatchdog)
                                                          createBucket(...) → setTimeout(60s, 150s)
                                                          → inbox.resync()    ◄── reclassify watchdog

lib/sse.ts (singleton):
   subscribeSse(handler) → adds to _handlers; lazily _open()
   _open: new EventSource('/api/sse'); onmessage parses JSON; broadcasts
   onerror: notify all + _close(); microtask reopen if handlers remain

lib/api.ts: thin fetch wrappers; 401 → throws {kind:'unauthorized'}
```

---

## 4. Environments

There is one repo with two language toolchains. Dependencies and runtime environments are scoped per process.

### 4.1 Backend Python environment (shared by API, worker, beat)

- **Manifest**: `server/pyproject.toml`, `server/uv.lock`.
- **Tooling**: `uv` (Astral). Python `>=3.13`.
- **Runtime libs**: `fastapi`, `uvicorn[standard]`, `celery[redis]`, `redis`, `sqlalchemy`, `psycopg[binary]`, `alembic`, `google-api-python-client`, `google-auth`, `google-auth-oauthlib`, `httpx`, `cryptography`, `itsdangerous`, `pydantic-settings`, `anthropic`.
- **Dev libs**: `pytest`, `pytest-asyncio`, `fakeredis`.
- **Python image**: `python:3.13-slim` (Dockerfile stage 2).
- **Env vars consumed** (`app/config.py`): `DATABASE_URL`, `REDIS_URL`, `SESSION_SECRET`, `ENCRYPTION_KEY`, `SESSION_TTL_SECONDS`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `COOKIE_DOMAIN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_CLASSIFY_MODEL`, `ANTHROPIC_CONCURRENCY`, `ENV`. (`PORT` is read by the Dockerfile CMD / uvicorn, not by `config.py`.) Worker honours `CELERY_TASK_ALWAYS_EAGER` for tests.
- **All three Python services share the same image**. The CMD differs (uvicorn vs `celery worker` vs `celery beat`); only the API container runs `alembic upgrade head` at boot.

### 4.2 Frontend / build-time environment

- **Manifest**: `client/package.json`, `client/bun.lock`.
- **Tooling**: `bun 1.3` (Dockerfile stage 1).
- **Runtime libs**: `react`, `react-dom` (v19).
- **Dev libs**: `vite`, `@vitejs/plugin-react`, `typescript`, `@types/react`, `@types/react-dom`.
- **Env vars consumed at build**: `VITE_OUT_DIR` (defaults to `../server/app/static`).
- **Vite dev proxy** (`client/vite.config.ts`): forwards `/auth` and `/api` to `localhost:8000` on dev port 5173.
- **Output**: emitted to `server/app/static/` so the production Python image can serve it without extra copy steps.

### 4.3 Local dev environment (docker-compose)

- `postgres:16` on `:5432` with healthcheck.
- `redis:7-alpine` on `:6379` with healthcheck.
- Volumes: `pgdata`.
- App processes run on the host (uv + bun) against these.

### 4.4 Production environment (Railway)

- Three Railway services from one Dockerfile, distinguished by `railway.toml` / `railway.beat.toml` / `railway.worker.toml`:
  - **API**: healthcheck `/api/health`, restart on failure, max 5 retries.
  - **Beat**: `numReplicas = 1` enforced.
  - **Worker**: `--concurrency=4`, restart on failure.
- Managed Postgres + managed Redis injected via `DATABASE_URL` / `REDIS_URL`.
- Secrets injected as env vars (no .env file in image; Dockerfile build asserts no secret-shaped strings made it into the SPA bundle).

### 4.5 Test environment

- `pytest` runs against `server/tests`. `asyncio_mode = "auto"` per `pyproject.toml`.
- `fakeredis` substitutes redis; `CELERY_TASK_ALWAYS_EAGER=1` makes celery synchronous so the task body runs inside the test transaction.

---

## 5. External service dependencies

Everything in the runtime topology that lives outside this repo.

### 5.1 Google OAuth 2.0 (accounts.google.com / oauth2.googleapis.com)

- **Used by**: API process — `app/auth/google_oauth.py` (auth flow) and `app/gmail/client.py` (refresh).
- **Role**: Authorise users; mint access + refresh tokens. `access_type=offline`, `prompt=consent`, scopes include `gmail.readonly`, `userinfo.email`, `userinfo.profile`, `openid`.
- **Channel**: HTTPS via `google-auth-oauthlib` `Flow` and `google.auth.transport.requests.Request`.
- **Failure mode**: `access_denied` → redirect to `/?authError=denied`.

### 5.2 Google userinfo v2

- **Used by**: API process (only at OAuth callback) — `_fetch_userinfo` in `google_oauth.py`.
- **Role**: Fetch authenticated user's email + display name to upsert the `users` row.

### 5.3 Gmail v1 API (gmail.googleapis.com)

- **Used by**: Worker process (most calls) + API process (only `/api/gmail/profile`).
- **Calls used**:
  - `users.getProfile` — profile probe.
  - `users.messages.list/get` — profile probe (last 3 subjects).
  - `users.threads.list?maxResults=200[&q=before:…]` — full sync + extend.
  - `users.threads.get?format=full` — detail fetch (full sync, partial sync, extend, draft-preview scoring, reclassify).
  - `users.history.list?startHistoryId=…&historyTypes=messageAdded` — partial sync. 404 on expired cursor → `HistoryGoneError`.
- **Channel**: HTTPS via `googleapiclient.discovery.build('gmail', 'v1', ...)`.

### 5.4 Anthropic Messages API (api.anthropic.com)

- **Used by**: Worker process — `app/llm/client.py` via `AsyncAnthropic`.
- **Role**: Classify threads against bucket criteria (`app/llm/prompts/classify_thread.py`) and score threads against draft criteria (`app/llm/prompts/score_thread.py`). Default model `claude-haiku-4-5`.
- **Channel**: HTTPS. Per-process semaphore caps concurrency at `ANTHROPIC_CONCURRENCY`.
- **Failure mode**: `call_messages` swallows exceptions and returns `""` so a single per-thread failure degrades to "no fit" rather than crashing a batch.

### 5.5 Postgres (Railway managed in prod, local container in dev)

- **Used by**: API + worker.
- **Role**: System of record for `users`, `sessions`, `buckets`, `inbox_threads`, `inbox_messages`. Encrypted Gmail tokens (Fernet) live in `users`.
- **Channel**: TCP via `psycopg` + SQLAlchemy (`postgresql+psycopg://`).

### 5.6 Redis (Railway managed in prod, local container in dev)

- **Used by**: API + worker + beat.
- **Roles**: celery broker + result backend; pubsub channels `user:{uid}`; key/value (`active_users` zset, `sync_lock:{uid}`, `preview:{draft_id}`).
- **Channel**: TCP via `redis-py` (sync) + `redis.asyncio` (async, used in API for the pubsub dispatcher).

### 5.7 Railway platform

- **Used by**: deploy/operate three services (API, worker, beat) and the two managed datastores.
- **Role**: builds the Dockerfile, injects env, runs healthchecks, restarts on failure. Build-time secret-leak guard runs inside Dockerfile stage 1.

### 5.8 (Build-time only) Astral uv installer (astral.sh)

- **Used by**: Docker build (one-time `curl … | sh` to install `uv`).
- **Role**: not a runtime dependency — present here for completeness of the runtime topology graph at build time.

### 5.9 (Build-time only) Bun (oven/bun:1.3)

- **Used by**: Docker build stage 1.
- **Role**: install client deps and build the SPA bundle.
