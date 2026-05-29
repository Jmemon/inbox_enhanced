---
name: reference-lookup
description: >-
  Find and load the right reference docs from reference/ before starting work on
  inbox_enhanced. Reference docs are dense navigational indexes of subsystems —
  FastAPI routes, SQLAlchemy models, Celery tasks/beat, the Gmail sync engine,
  the Anthropic LLM classify/score paths, the Redis pubsub/SSE realtime layer,
  Google OAuth/sessions, and the React/Vite SPA. Use when starting a spec or
  task, debugging across files/processes, exploring an unfamiliar subsystem, or
  making a change that spans the API ↔ worker ↔ Redis ↔ browser boundaries.
---

# Reference Lookup

Load the map before the territory. Before touching a subsystem, find and read the
reference doc(s) that index it, so you start with file paths, types, services,
routes, and data flows already in context instead of rediscovering them mid-task.

This skill **surfaces and loads** the right docs. It does not replace reading them —
your job ends only when you have read the matched docs in full.

## What reference docs are

Reference docs in `reference/` are **dense navigational indexes**, not prose or
tutorials. Each maps one subsystem: file paths, key exports/types, services it
talks to, routes/tasks it owns, and the data flows across process boundaries
(API ↔ Postgres ↔ Redis ↔ Worker ↔ Google/Anthropic ↔ browser). If something in
`reference/` reads like a walkthrough, it is the wrong artifact.

The master index is **`reference/MANIFEST.md`**. It is a table:

| Column | Meaning |
|--------|---------|
| **File** | the reference doc path under `reference/` |
| **Stamp** | short commit SHA + branch + date the doc was last validated against code |
| **Scope** | one-line description of what the doc covers — **this is what you match your task against** |

`ARCHITECTURE.md` (repo root) is the system-wide map of processes, inter-process
edges, code DAGs, environments, and external dependencies. It is the fallback /
companion to any reference doc and is itself stamped at the top.

## When to use

- Starting a new spec or task (anything in `specs/`).
- Debugging behavior that crosses files or processes (e.g. an SSE event not
  arriving, a sync that didn't fire, a bucket reclassify that didn't update).
- Exploring a subsystem you haven't touched before.
- Any change spanning multiple layers: an endpoint that enqueues a Celery task
  that publishes to Redis that the browser consumes over SSE is the *normal* case
  here, not the exception.
- Before writing a migration, a new route, a new Celery task, or a new LLM prompt.

## How to use

1. **Read `reference/MANIFEST.md` first.** Scan the **Scope** column.
2. **Match your task to Scope, not filename.** Pick every doc whose Scope overlaps
   any subsystem your task will read or change. Use the lookup table below as the
   routing aid.
3. **Apply the cross-cutting rule** (below) — almost every task pulls in the API,
   data, and realtime indexes whether or not you named them.
4. **Read the matched docs in full** before writing code. Focus on: file paths and
   key exports, the data flow across process boundaries, the cross-subsystem
   touchpoints (what enqueues what, what publishes what, what the browser polls
   vs. receives over SSE), and decision points (locks, cursors, thresholds).
5. **If no doc matches**, see "If no doc matches" below — do not silently proceed.

## Task → doc lookup table

Build your read-set by matching the task to the **Task involves...** column, then
confirm against each candidate doc's Scope in the manifest. Bias toward
over-inclusion: reading an extra dense index is far cheaper than missing a
dependency and debugging it later.

> NOTE: The reference corpus is being bootstrapped. Many target docs below do not
> exist yet — they are the planned decomposition. When a needed doc is absent,
> treat it as a backlog item (see "If no doc matches") and fall back to the
> relevant section of `ARCHITECTURE.md`.

| Task involves... | Reference doc(s) | Until authored, fall back to |
|------------------|------------------|------------------------------|
| HTTP routes, request/response shapes, FastAPI routers, `app.deps.get_current_user`, status codes, SPA catch-all, StaticFiles mount | `API_INDEX.md` (covers `app/main.py`, `app/api/{auth,buckets,inbox,gmail,sse}.py`, `app/deps.py`) | ARCHITECTURE.md §1.1, §3.1 |
| Persistence, SQLAlchemy models, tables (`users`, `sessions`, `buckets`, `inbox_threads`, `inbox_messages`), repos, Alembic migrations, unique constraints, session engine | `DATA_INDEX.md` (covers `app/db/{models,session}.py`, `app/inbox/{bucket_repo,inbox_repo}.py`, `server/migrations/`) | ARCHITECTURE.md §2.2–2.3, §5.5 |
| Realtime delivery: SSE endpoint, the pubsub dispatcher, redis pubsub channels `user:{uid}`, `active_users` zset, `sync_lock`, in-tab EventSource, event replay (`/api/threads/batch`) | `REALTIME_INDEX.md` (covers `app/realtime/*`, `app/api/sse.py`, `client/src/lib/sse.ts`, `useInboxSse.tsx`) | ARCHITECTURE.md §1.5, §2.4, §2.7, §3.1 (SSE) |
| Celery tasks, beat schedule, the worker process, `apply_async` call sites, eager-mode tests, lock/retry semantics | `WORKERS_INDEX.md` (covers `app/workers/{celery_app,beat_schedule,tasks,gmail_sync}.py`) | ARCHITECTURE.md §1.2–1.3, §3.2–3.3 |
| Gmail sync engine: full/partial/extend sync, history cursor (`gmail_last_history_id`), `HistoryGoneError`, `threads.list/get`, `users.history.list`, message parsing | `GMAIL_SYNC_INDEX.md` (covers `app/workers/gmail_sync.py`, `app/gmail/{client,parser}.py`) | ARCHITECTURE.md §3.2, §5.3 |
| LLM classification/scoring: Anthropic client, the worker-process LLM loop thread, `ANTHROPIC_CONCURRENCY` semaphore, classify vs. score prompts, default bucket criteria | `LLM_INDEX.md` (covers `app/llm/{client,classify,default_criteria}.py`, `app/llm/prompts/*`) | ARCHITECTURE.md §2.10, §5.4 |
| Auth & sessions: Google OAuth flow, token encryption (Fernet), session cookies, state cookie, `ensure_fresh_access_token`, user upsert | `AUTH_INDEX.md` (covers `app/auth/{google_oauth,crypto,sessions,state_cookie}.py`, `app/api/auth.py`) | ARCHITECTURE.md §2.1, §2.8, §5.1–5.2 |
| Buckets domain: bucket CRUD, criteria formulation, draft preview scoring + cache, reclassify, default buckets | `BUCKETS_INDEX.md` (covers `app/api/buckets.py`, `app/inbox/{bucket_repo,preview_cache}.py`, reclassify/draft-preview tasks) | ARCHITECTURE.md §3.1 (buckets), §3.2 (draft_preview/reclassify) |
| React SPA: components/hooks/routing, `useInbox`/`useBuckets`/`useAuth`, pagination, auto-extend, watchdogs, modals, `lib/api.ts` fetch wrappers | `CLIENT_INDEX.md` (covers `client/src/**`) | ARCHITECTURE.md §1.6, §3.4 |
| Config/env vars, deployment, Docker, Railway services, docker-compose, build pipeline (Vite/Bun, uv) | `OPS_INDEX.md` (covers `app/config.py`, `Dockerfile`, `railway*.toml`, `docker-compose.yml`, `client/vite.config.ts`) | ARCHITECTURE.md §4, §5.7–5.9 |

## Cross-cutting rule — "when in doubt, also read X"

These indexes are co-relevant with almost everything. Add them to your read-set
even if the task didn't name them:

- **Any change touching an HTTP endpoint** → also read **`API_INDEX.md`**.
- **Any change touching persistence or shared state** → also read **`DATA_INDEX.md`**
  (Postgres/models) and, if state crosses processes, **`REALTIME_INDEX.md`**
  (Redis pubsub / `active_users` / `sync_lock` / SSE).
- **Any change to a Celery task or to code a task calls** → also read
  **`WORKERS_INDEX.md`**, because a single user action commonly threads
  API → Celery → Redis → SSE → browser.
- When you cannot decide whether a doc is relevant, **include it.** Over-inclusion
  is cheap; a missed dependency is not.

## Staleness — a signal, not a verdict

Every reference doc carries a top-of-file stamp:

```
<!-- stamp: <short-sha> (<branch>) | <YYYY-MM-DD> -->
```

(`ARCHITECTURE.md` uses the prose form "Stamped at commit `<sha>` on branch
`<branch>`.") Compare the stamp's SHA to `git log -1 --format=%h`:

- **Stamp is current or a few commits behind** → trust the paths, types, routes,
  and data flows as directionally correct. Proceed.
- **Stamp is clearly old** (many commits / the named subsystem has obviously moved)
  → still use the doc for orientation, but **verify specific details against the
  current code** before relying on them, and re-stamp the doc as part of your work.
- **No stamp** → the doc cannot be trusted for staleness. Treat it as a defect:
  verify against code and add a stamp.

There is currently **no CI/pre-commit staleness hook** in this repo, so judging
staleness is the agent's responsibility on every read.

## If no doc matches

A subsystem with no reference doc is a backlog item, not a dead end:

1. Author the doc using **`reference/prompts/CREATE_INDEX.md`** (new subsystem
   index) or extend an existing one via **`reference/prompts/ADD_REFERENCE.md`**.
   Both require the resulting doc to carry the stamp above — and to commit any
   referenced code first, then stamp, so the stamp is meaningful.
2. Add a row to `reference/MANIFEST.md` (File | Stamp | Scope).
3. In the meantime, fall back to the matching section of `ARCHITECTURE.md` (see the
   "fall back to" column above) and read the actual source files for that subsystem.
4. Surface the gap to the user so the corpus grows over time.
