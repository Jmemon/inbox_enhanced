# Prompt: Create a Reference Index for a Subsystem of inbox_enhanced

Use this to author a **new** reference doc for one subsystem and register it in
`reference/MANIFEST.md`. A reference doc is a **dense navigational index** — file
paths, key exports/types, services, routes/tasks, and cross-process data flows —
**not** a tutorial or prose walkthrough.

## Inputs you need

- **Subsystem name** and the set of files it owns (e.g. the Celery workers:
  `server/app/workers/{celery_app,beat_schedule,tasks,gmail_sync}.py`).
- The repo's process model — see `ARCHITECTURE.md` (§1 processes, §2 inter-process
  edges, §3 code DAGs). Reuse its terminology so docs compose.

## Stack facts to honor

- **Python**: managed with `uv` (Python ≥ 3.13). FastAPI + uvicorn (API),
  Celery + beat (workers), SQLAlchemy + Alembic + psycopg (Postgres),
  redis-py / redis.asyncio (Redis), Anthropic SDK (LLM), google-api-python-client
  (Gmail/OAuth). Never hand-edit `pyproject.toml`; use `uv` commands.
- **JS/TS**: managed with `bun`. React 19 + Vite SPA under `client/`, built into
  `server/app/static/`.
- Do **not** read `.env` files. Document env-var *names* only (from `app/config.py`).

## Steps

1. **Commit referenced code first.** A reference doc describes code. If any code
   it will cite is uncommitted, commit it first so the stamp is meaningful. An
   unstamped or stale-stamped doc is a defect.

2. **Capture the stamp.** Run:
   ```
   git log -1 --format=%h        # short SHA
   git rev-parse --abbrev-ref HEAD   # branch
   date +%F                       # YYYY-MM-DD
   ```

3. **Write the doc** at `reference/<SUBSYSTEM>_INDEX.md` with this shape:

   ```markdown
   <!-- stamp: <short-sha> (<branch>) | <YYYY-MM-DD> -->

   # <Subsystem> Index

   > Scope: <one line — the same text you put in the manifest Scope column>

   ## Files
   | Path | Role / key exports |
   |------|--------------------|
   | server/app/.../x.py | <functions, classes, what it owns> |

   ## Routes / Tasks / Entrypoints
   <The HTTP routes, Celery tasks, beat entries, or React hooks/components this
   subsystem owns, with their trigger and a one-line behavior.>

   ## Data & state touched
   <Postgres tables, Redis keys/channels (active_users, sync_lock:{uid},
   preview:{draft_id}, pubsub user:{uid}), external services (Gmail v1, Anthropic,
   Google OAuth). Note read vs. write.>

   ## Data flows / cross-subsystem touchpoints
   <Use ARCHITECTURE.md notation: A ──[channel: data]──> B. Show what enqueues
   what, what publishes what, and what the browser polls vs. receives over SSE.>

   ## Decision points & gotchas
   <Locks, history-cursor expiry / HistoryGoneError, LWW gates, score thresholds,
   ordering constraints (e.g. cache-store-before-publish), eager-mode test behavior.>
   ```

4. **Keep it dense.** Paths, types, routes, flows. If a section reads like prose,
   compress it into a table or bullet list. The doc is a map for relocating code,
   not an explanation of it.

5. **Register it.** Add a row to `reference/MANIFEST.md`:
   `| <SUBSYSTEM>_INDEX.md | <short-sha> (<branch>) | <YYYY-MM-DD> | <one-line scope> |`
   The Scope line is what agents match tasks against — make it specific and keyword-rich.

6. **Re-stamp on material change.** If you later validate/update the doc against a
   newer commit, update both the top-of-file stamp and the manifest row.
