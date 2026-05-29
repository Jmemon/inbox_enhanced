# Prompt: Add To / Update a Reference Index of inbox_enhanced

Use this when a subsystem **already has** a reference doc and you need to extend,
correct, split, or re-validate it — not to create a brand-new index (use
`CREATE_INDEX.md` for that). The goal is to keep the corpus accurate and re-stamped
so the `reference-lookup` skill can trust it.

## When to use

- Code in an indexed subsystem moved, changed signature, or gained/lost a route,
  Celery task, model, Redis key, or React hook.
- An agent following the `reference-lookup` skill found a doc's stamp is clearly
  stale and verified drift against current code.
- A doc has grown too broad and should be split into a more specific index, OR a
  small adjacent area should fold into an existing doc rather than spawn a new one.
- A doc is missing its top-of-file stamp (a defect) and must be re-stamped.

## Stack facts to honor

- **Python** via `uv` (≥ 3.13): FastAPI/uvicorn, Celery + beat, SQLAlchemy +
  Alembic + psycopg, redis-py / redis.asyncio, Anthropic SDK, google-api-python-client.
  Never hand-edit `pyproject.toml`; use `uv`.
- **JS/TS** via `bun`: React 19 + Vite under `client/`.
- Never read `.env`. Document env-var *names* only (from `app/config.py`).
- Keep docs **dense** — paths, types, routes, flows. Reuse `ARCHITECTURE.md`
  terminology and the `A ──[channel: data]──> B` flow notation.

## Steps

1. **Commit referenced code first.** If your edit cites code that isn't committed,
   commit it before stamping — the stamp must point at code that exists in history.

2. **Read the current doc and the live code** it indexes. Reconcile every path,
   export, route/task, table, Redis key/channel, and data-flow edge against the
   source. Fix drift; add what's new; remove what's gone.

3. **Decide scope shape (YAGNI):**
   - Small addition that fits the subsystem → extend the existing doc.
   - Distinct subsystem that crept in → split it out via `CREATE_INDEX.md` and
     leave a pointer.
   Do not speculatively over-document areas the change didn't touch.

4. **Re-stamp.** Capture:
   ```
   git log -1 --format=%h            # short SHA
   git rev-parse --abbrev-ref HEAD   # branch
   date +%F                          # YYYY-MM-DD
   ```
   Update the top-of-file stamp to
   `<!-- stamp: <short-sha> (<branch>) | <YYYY-MM-DD> -->`. An unstamped doc cannot
   be judged for staleness and is a defect.

5. **Update `reference/MANIFEST.md`.** Refresh the doc's Stamp column to match the
   new top-of-file stamp. If the Scope changed (subsystem grew/shrank), update the
   Scope line too — it is the field agents match tasks against, so keep it specific
   and keyword-rich. If you split a doc, add the new row.

6. **Sanity check.** Every path the doc names should exist; every route/task/model
   it claims should be real. No phantom references. If the doc now reads like prose,
   compress it back into tables/bullets — it is a map, not a tutorial.
