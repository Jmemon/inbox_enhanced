<!-- stamp: 13a07e5 (main) | 2026-05-29 -->

# Generate Reference-Lookup Skill for inbox_enhanced

## Context

`inbox_enhanced` is a Gmail inbox-enhancement app with two language toolchains in
one repo:

- **Backend (Python, `uv`, ≥3.13)** under `server/app/`: a FastAPI/uvicorn **API**
  process, a **Celery worker** process, and a **Celery beat** process — all from
  one Docker image, differing only in CMD. Postgres (SQLAlchemy + Alembic +
  psycopg) is the system of record; Redis plays three roles (Celery broker/backend,
  pubsub channels `user:{uid}`, and key/value store: `active_users` zset,
  `sync_lock:{uid}`, `preview:{draft_id}`). External services: Google OAuth, Gmail
  v1, Anthropic Messages API.
- **Frontend (JS/TS, `bun`)** under `client/`: a React 19 + Vite SPA, built into
  `server/app/static/` and served by the API via StaticFiles + an SPA catch-all.

The authoritative system map is **`ARCHITECTURE.md`** at the repo root (processes,
inter-process edges, code DAGs, environments, external deps), stamped
`13a07e5 (main)`.

**Reference corpus status:** There is **no reference-doc corpus yet.** The
`reference/` directory exists but is empty — `reference/assignment.md` was recently
deleted. Therefore the reference-lookup skill is inert until a corpus grows. Per
the meta-prompt's Step 1f, this work **bootstraps a minimal scaffold** (manifest +
authoring prompts) alongside the skill — it does NOT speculatively author the whole
corpus.

**Manifest format (chosen for this repo):** `reference/MANIFEST.md`, a table with
columns **File | Stamp | Scope**. Scope is the one-line field agents match a task
against. Stamp format is `<short-sha> (<branch>) | <YYYY-MM-DD>`.

**Staleness mechanism:** Each reference doc carries a top-of-file stamp
`<!-- stamp: <short-sha> (<branch>) | <YYYY-MM-DD> -->` (ARCHITECTURE.md uses the
prose variant). There is **no CI/pre-commit staleness hook**, so the agent judges
staleness on each read by comparing the stamp's SHA to `git log -1 --format=%h`.

**Skill installation path:** `.claude/skills/reference-lookup/SKILL.md`.

**Subsystem decomposition** (the basis for the task→doc lookup table), derived from
`server/app/` and `client/src/`:

| Subsystem | Owns | Planned index doc |
|-----------|------|-------------------|
| API / routes | `app/main.py`, `app/api/{auth,buckets,inbox,gmail,sse}.py`, `app/deps.py` | `API_INDEX.md` |
| Data / persistence | `app/db/{models,session}.py`, `app/inbox/{bucket_repo,inbox_repo}.py`, `server/migrations/` | `DATA_INDEX.md` |
| Realtime (SSE/pubsub) | `app/realtime/*`, `app/api/sse.py`, `client/src/lib/sse.ts`, `useInboxSse.tsx` | `REALTIME_INDEX.md` |
| Workers (Celery/beat) | `app/workers/{celery_app,beat_schedule,tasks,gmail_sync}.py` | `WORKERS_INDEX.md` |
| Gmail sync engine | `app/workers/gmail_sync.py`, `app/gmail/{client,parser}.py` | `GMAIL_SYNC_INDEX.md` |
| LLM classify/score | `app/llm/{client,classify,default_criteria}.py`, `app/llm/prompts/*` | `LLM_INDEX.md` |
| Auth / sessions | `app/auth/{google_oauth,crypto,sessions,state_cookie}.py`, `app/api/auth.py` | `AUTH_INDEX.md` |
| Buckets domain | `app/api/buckets.py`, `app/inbox/{bucket_repo,preview_cache}.py` | `BUCKETS_INDEX.md` |
| Client SPA | `client/src/**` | `CLIENT_INDEX.md` |
| Ops / config | `app/config.py`, `Dockerfile`, `railway*.toml`, `docker-compose.yml`, `client/vite.config.ts` | `OPS_INDEX.md` |

Cross-cutting (near-always relevant) indexes: **API_INDEX**, **DATA_INDEX**,
**REALTIME_INDEX**, **WORKERS_INDEX** — because a single user action commonly
threads API → Celery → Redis → SSE → browser.

---

## What to create

### File 1: `.claude/skills/reference-lookup/SKILL.md`

Create the skill with YAML frontmatter (`name: reference-lookup`, and a
`description` naming `reference/`, the manifest, and the indexed subsystems so it
triggers when starting specs/tasks, debugging across files/processes, exploring an
unfamiliar subsystem, or making multi-layer changes across the API ↔ worker ↔
Redis ↔ browser boundaries).

Body sections, in order:

1. **Header principle** — "Load the map before the territory"; the skill surfaces
   and loads docs but does not replace reading them.
2. **What reference docs are** — dense navigational indexes (paths, types,
   services, routes, flows), not prose; the master index is `reference/MANIFEST.md`
   (columns File | Stamp | Scope); `ARCHITECTURE.md` is the system-wide fallback.
3. **When to use** — the universal triggers plus repo-specific ones (before a
   migration, new route, new Celery task, or new LLM prompt).
4. **How to use** — read MANIFEST first; match on **Scope, not filename**; apply
   cross-cutting rule; read matched docs in full (focus on file paths/exports,
   cross-process data flows, decision points like locks/cursors/thresholds); if no
   doc matches, follow the authoring path.
5. **Task → doc lookup table** — one row per subsystem above. Each row: trigger
   keywords → planned index doc → an explicit "until authored, fall back to"
   `ARCHITECTURE.md` section, because no docs exist yet. Include a note that the
   corpus is being bootstrapped and absent docs are backlog items.
6. **Cross-cutting rule** — "when in doubt, also read X": endpoint changes pull in
   API_INDEX; persistence/shared-state changes pull in DATA_INDEX (+ REALTIME_INDEX
   if state crosses processes); Celery-touching changes pull in WORKERS_INDEX; when
   undecided, include the doc.
7. **Staleness** — describe the stamp format; current/few-behind → trust;
   clearly-old → verify against code and re-stamp; no stamp → defect. Note there is
   no CI hook, so staleness is the agent's call each read.
8. **If no doc matches** — author via `reference/prompts/CREATE_INDEX.md` or extend
   via `ADD_REFERENCE.md` (both require committing referenced code first, then
   stamping), add a MANIFEST row, fall back to `ARCHITECTURE.md` meanwhile, and
   surface the gap to the user.

The authoritative content for this file is exactly what was written to
`.claude/skills/reference-lookup/SKILL.md` in this repo — reproduce/maintain it
there.

### Companion scaffold (bootstrapped — no corpus exists)

#### `reference/MANIFEST.md`

Top-of-file stamp; a header explaining the **matching contract** (select by Scope,
not filename; bias to over-inclusion; the skill automates routing); column
definitions for File / Stamp / Scope; an empty index table with a
"_(none yet — corpus is being bootstrapped)_" placeholder row; an Authoring section
pointing at the two prompts and restating the stamp requirement (commit referenced
code first); and a pointer to `ARCHITECTURE.md` as the fallback.

#### `reference/prompts/CREATE_INDEX.md`

Authoring prompt for a NEW subsystem index, adapted to this Python(`uv`)/JS(`bun`)
stack. Must: require committing referenced code first; capture stamp via
`git log -1 --format=%h`, `git rev-parse --abbrev-ref HEAD`, `date +%F`; produce a
doc at `reference/<SUBSYSTEM>_INDEX.md` carrying the
`<!-- stamp: <short-sha> (<branch>) | <YYYY-MM-DD> -->` stamp with sections
Files / Routes-Tasks-Entrypoints / Data-and-state-touched / Data-flows / Decision-
points; mandate density (tables, not prose) reusing ARCHITECTURE.md's
`A ──[channel: data]──> B` notation; register a MANIFEST row; forbid reading `.env`
and hand-editing `pyproject.toml`.

#### `reference/prompts/ADD_REFERENCE.md`

Authoring prompt for EXTENDING / UPDATING / re-validating an existing index. Must:
commit referenced code first; reconcile every path/export/route/task/table/Redis
key/data-flow against live code; decide extend-vs-split (YAGNI); re-stamp top-of-
file and refresh the MANIFEST Stamp (and Scope, if changed); sanity-check for
phantom references; same stack constraints (`uv`/`bun`, no `.env`).

---

## After creating the skill files

The corpus is empty, so the skill is inert until seeded. Per YAGNI, do NOT author
the full corpus. Author the **1–2 most-touched subsystems first** using
`reference/prompts/CREATE_INDEX.md` (git history over the last ~50 commits shows the
client pages and the Celery workers as the hottest areas):

1. **`WORKERS_INDEX.md`** — `app/workers/{celery_app,beat_schedule,tasks,gmail_sync}.py`
   (and the Gmail sync engine it drives). This is the highest-leverage seed: it is a
   cross-cutting index, the most complex process, and the hub of the
   API → Celery → Redis → SSE → browser flow.
2. **`CLIENT_INDEX.md`** — `client/src/**` (hooks `useInbox`/`useInboxSse`/
   `useBuckets`/`useAuth`, pagination + auto-extend + watchdogs, modals, `lib/api.ts`,
   `lib/sse.ts`). The most-churned area by commit count.

Each seed doc must carry the stamp and get a MANIFEST row. Everything else (API,
DATA, REALTIME, AUTH, BUCKETS, LLM, GMAIL_SYNC, OPS indexes) is authored on demand
the first time a task touches an unindexed subsystem.
