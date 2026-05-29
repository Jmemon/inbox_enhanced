# Generate AGENTS.md for inbox_enhanced

## Context

`inbox_enhanced` is a Gmail inbox-enhancement app: a FastAPI server + a React 19 /
Vite TypeScript SPA, Celery workers (worker + beat) that do background Gmail sync,
Postgres for state, Redis serving three roles (Celery broker/result-backend, an
active-user registry, and a per-user pub/sub bus), and an Anthropic LLM classifier
that sorts inbox threads into user-defined buckets.

Repo shape: two packages under one repo root — `server/` (Python 3.13, managed with
`uv`) and `client/` (React/Vite/TS, managed with `bun`). One shared Dockerfile image
deploys as three Railway services (api, worker, beat). It is **not** a JS/TS
workspace monorepo; the two packages are coupled only by the frontend build emitting
into `server/app/static/`.

Agent tools in use: **Claude Code only** → create `CLAUDE.md -> AGENTS.md`. No
evidence of Gemini → do NOT create `GEMINI.md`.

`AGENTS.md` is the single root orientation doc; it must be terse, factual, and
navigational (a stamped tree + arrow-chain relationships), not prose. Detail lives
in `ARCHITECTURE.md`, the `reference/` index docs, and the `reference-lookup` skill.

## What to create

### File 1: `<repo-root>/AGENTS.md`

Write the file below verbatim (it is already adapted to this repo's real paths,
ports, services, and components). Re-stamp the repo map per the "After creating the
file" section.

````markdown
# AGENTS.md — inbox_enhanced

Gmail inbox-enhancement app: FastAPI server + React/Vite SPA, Celery workers for
background Gmail sync, Postgres for state, Redis as broker/result-backend/pub-sub,
and an Anthropic LLM classifier that sorts threads into user-defined buckets.

## Standing instructions

- Python: manage with `uv` only — never hand-edit `server/pyproject.toml`; use `uv add`/`uv run`. Python 3.13+.
- JS/TS: manage with `bun` only (`client/`). React 19, Vite, TypeScript.
- Run the full local stack with `scripts/dev.sh` (docker pg+redis, then api :8000, vite :5173, celery worker + beat).
- Ports: FastAPI :8000, Vite dev :5173 (proxies `/auth` + `/api` → :8000), Postgres :5432, Redis :6379. Check availability before reassigning.
- Never read `.env` (secrets). Only `.env.example`.
- DB migrations are Alembic (`server/migrations/`); apply with `uv run alembic upgrade head` from `server/`.
- Beat MUST run as a single replica — multiple beats multiply Gmail fan-outs (see `railway.beat.toml`).
- Frontend build emits into `server/app/static/` (gitignored) — never edit by hand; rebuild via `scripts/build_frontend.sh`.
- Tests: `cd server && uv run pytest`; set `CELERY_TASK_ALWAYS_EAGER=1` to run tasks synchronously.
- Before touching a subsystem, use the `reference-lookup` skill (`.claude/skills/reference-lookup/`) to load the matching `reference/` index docs (routed via `reference/MANIFEST.md`).
- Write descriptive comments for major changes; YAGNI.

## Repository Map

<!-- repo-map-stamp: <short-sha> (<branch>) | <YYYY-MM-DD> — <note> -->

```
inbox_enhanced/
├── AGENTS.md                       # THIS FILE — repo orientation + agent instructions
├── CLAUDE.md -> AGENTS.md          # Symlink so Claude Code reads these instructions
├── ARCHITECTURE.md                 # Long-form process/code-layer map (read for deep detail)
├── README.md                       # Local-dev setup instructions
├── docker-compose.yml              # Local Postgres :5432 + Redis :6379
├── Dockerfile                      # Shared image: api default CMD = alembic upgrade + uvicorn
├── railway.toml                    # Railway api service (healthcheck /api/health)
├── railway.worker.toml             # Railway celery worker service (concurrency 4)
├── railway.beat.toml               # Railway celery beat service — numReplicas MUST be 1
├── .env.example                    # Env template (NEVER read real .env)
├── client/                         # React 19 / Vite / TS SPA (manage with bun)
│   ├── package.json                # bun deps; scripts: dev/build/preview
│   ├── vite.config.ts              # :5173, proxies /auth+/api → :8000, builds to ../server/app/static
│   └── src/
│       ├── main.tsx                # SPA entry
│       ├── App.tsx                 # Root component / routing
│       ├── auth/                   # Login, Splash, useAuth hook (session-cookie auth)
│       ├── lib/                    # api.ts (fetch wrapper), sse.ts (EventSource client)
│       └── pages/
│           ├── Home.tsx            # Authed home shell
│           ├── inbox/              # InboxList, Pagination, ReloadButton, useInbox, useInboxSse
│           └── buckets/            # Bucket CRUD + filter dropdown modals + useBuckets
├── server/                         # FastAPI app (manage with uv; Python 3.13)
│   ├── pyproject.toml              # uv-managed deps — do NOT hand-edit
│   ├── migrations/                 # Alembic env + versions/ (apply: uv run alembic upgrade head)
│   ├── tests/                      # pytest (asyncio auto); conftest wires in-memory db + fakeredis
│   └── app/
│       ├── main.py                 # FastAPI app: routers, lifespan (pubsub), SPA catch-all, /api/health
│       ├── config.py               # pydantic-settings Settings (env-driven)
│       ├── deps.py                 # FastAPI deps: get_db, get_current_user
│       ├── api/                    # HTTP routers: auth(/auth), inbox(/api), buckets(/api), gmail(/api/gmail), sse(/api)
│       ├── auth/                   # google_oauth, sessions, crypto (Fernet token encrypt), state_cookie
│       ├── db/                     # models.py (User/Session/Bucket/InboxThread/InboxMessage), session.py
│       ├── gmail/                  # client.py (Gmail API), parser.py (assemble/stringify threads)
│       ├── inbox/                  # inbox_repo, bucket_repo, preview_cache (Postgres read/write layer)
│       ├── llm/                    # classify.py, client.py (Anthropic), default_criteria.py, prompts/
│       │   └── prompts/            # classify_thread.py, score_thread.py
│       ├── realtime/               # redis_client, pubsub (per-worker dispatcher), sse_connections,
│       │                           #   active_users (online registry), sync_lock, active pub/sub bus
│       ├── workers/                # Celery: celery_app, beat_schedule (30s tick), tasks, gmail_sync
│       └── static/                 # GENERATED frontend bundle (gitignored) — never edit; build via scripts/
├── scripts/
│   ├── dev.sh                      # Full local stack: docker pg+redis, api, vite, worker, beat
│   ├── build_frontend.sh           # bun run build → server/app/static
│   ├── check_bundle_secrets.sh     # Guard: scan built bundle for leaked secrets
│   └── tail_railway.sh             # Stream multi-service Railway logs into one stdout
├── specs/                          # Design specs (UNTRACKED in git as of stamp)
│   ├── 001_project_minimum/        # v1 specs: api/client/flows/psql/redis/workers/auth/homepage/buckets
│   ├── 002_inbox_sync/             # Inbox sync flows, op triggers, storage, open questions
│   └── 003_task_hud/               # Task HUD flows
├── plans/                          # Implementation plans (auth, buckets, homepage)
├── reference/                      # Subsystem index docs (dense navigational maps, not tutorials)
│   ├── MANIFEST.md                 # Master index — match tasks to docs by Scope (corpus bootstrapping)
│   └── prompts/                    # CREATE_INDEX.md (+ ADD_REFERENCE.md) authoring prompts
└── .claude/
    ├── settings.local.json         # Local permission allowlist
    └── skills/
        └── reference-lookup/       # Skill: load reference/ index docs before working on a subsystem
```

## Key Relationships

- **Periodic Gmail sync (fan-out):** Celery beat (30s tick, `workers/beat_schedule.py`) → `tasks.enqueue_polls` → per-active-user `tasks.poll_new_messages` → `gmail_sync.fetch_history_records` (Gmail `users.history.list`; 404 → `HistoryGoneError` → `full_sync_inbox`) → `gmail_sync.partial_sync_inbox` writes `inbox_repo`/Postgres → publish via `realtime.pubsub`.
- **Active-user gating:** Only users with a live SSE connection are polled. SSE connect → `realtime.active_users` (Redis registry) → beat's `enqueue_polls` reads the registry to decide whom to poll.
- **Realtime push:** Worker writes Postgres → publishes to per-user Redis channel → each uvicorn worker's `PubSubDispatcher` (`realtime/pubsub.py`, started in `main.py` lifespan) routes to in-memory queues → `api/sse.py` `StreamingResponse` → client `lib/sse.ts` EventSource → `pages/inbox/useInboxSse`.
- **LLM classification:** Sync touches thread → `gmail/parser.thread_to_string` → `llm/classify.classify` (Anthropic `claude-haiku-4-5`, parallel under asyncio semaphore, `ANTHROPIC_CONCURRENCY`) using `llm/prompts/classify_thread.py` + user `Bucket` criteria → bucket assignment persisted via `inbox/bucket_repo`.
- **Auth:** `/auth` Google OAuth (`auth/google_oauth.py`) → refresh token Fernet-encrypted (`auth/crypto.py`, `ENCRYPTION_KEY`) into Postgres `users` → session cookie signed via `SESSION_SECRET` (`auth/sessions.py`, `state_cookie.py`); all API endpoints derive user from cookie (`deps.get_current_user`), including SSE (no path-based user id).
- **Redis: three roles, one instance** (`REDIS_URL`): Celery broker + result backend, active-user registry (`realtime/active_users.py`), per-user pub/sub bus + `sync_lock`.
- **Frontend serving:** Vite builds `client/` → `server/app/static/` (gitignored); FastAPI mounts `/assets` and a SPA catch-all in `main.py` serves `index.html` for non-`api/`/`auth/`/`assets/` paths. Dev mode instead runs Vite :5173 proxying `/auth`+`/api` → :8000.
- **Deploy:** One Dockerfile image, three Railway services — api (`railway.toml`, runs `alembic upgrade head` then uvicorn), worker (`railway.worker.toml`), beat (`railway.beat.toml`, single replica).
````

### Symlinks

After writing AGENTS.md, create the symlink from the repo root:

```bash
ln -sf AGENTS.md CLAUDE.md
# Do NOT create GEMINI.md — Gemini is not in use in this repo.
```

Verify: `ls -l CLAUDE.md` should show `CLAUDE.md -> AGENTS.md`.

If a real (non-symlink) `CLAUDE.md` already exists at the repo root, do NOT clobber
it — fold its content into AGENTS.md's preamble/relationships first, confirm with the
user, then replace it with the symlink. (At creation time there was no root
`CLAUDE.md`; the only `CLAUDE.md` is the user's global file elsewhere.)

## After creating the file

Stamp the repo map with the current commit, branch, and date. Replace the
`<!-- repo-map-stamp: ... -->` placeholder using:

- short SHA: `git -C <repo-root> log -1 --format=%h`
- branch: `git -C <repo-root> rev-parse --abbrev-ref HEAD`
- date: today's date (YYYY-MM-DD)

Stamp format:
`<!-- repo-map-stamp: <short-sha> (<branch>) | <YYYY-MM-DD> — <note> -->`

The meta-prompt says to commit documented-but-uncommitted structure before stamping.
The `specs/001_project_minimum/`, `specs/002_inbox_sync/`, and `specs/003_task_hud/`
dirs are currently UNtracked. If you are permitted to commit, commit them first, then
stamp. If commits are disallowed (working tree has pending user changes), instead
stamp with the current HEAD SHA and add a note like
`(working tree dirty — spec dirs untracked)`.

Do NOT run any mutating git command (`commit`/`add`) unless explicitly authorized.
Read-only git (`log`, `status`, `rev-parse`) is fine.
