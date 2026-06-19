<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 2 — data-layer first -->

# 004 Vision Architecture — Overview (Agent 2: data-layer first)

> Lens: **the data layer is the product's load-bearing wall.** Tasks are LLM
> projections over inbox data; if the data is stale, incomplete, or slow to
> query, every task is stale, incomplete, and slow. This plan builds the data
> foundation first — sync, storage/search, event propagation — and only then
> erects the task engine and HUD on top of explicit guarantees.

Companion files (read in order):

| File | Covers |
|------|--------|
| `agent-2-01-sync.md` | Gmail push sync (users.watch + Pub/Sub), label/delete mirroring, full-sync redesign, resolution of every `002_inbox_sync/05_18_26-open-questions.md` item |
| `agent-2-02-storage-search.md` | Full-body storage in Postgres, FTS (tsvector + pg_trgm), migration sketches against `server/app/db/models.py` |
| `agent-2-03-events.md` | Event propagation: Redis Streams event log + SSE resume, decoupled ingest/enrich events, named changes in `server/app/realtime/` |
| `agent-2-04-tasks.md` | Task data model + engine, positions on all four VISION.md open questions, bucket unification |
| `agent-2-05-api-frontend.md` | API surface, HUD-first frontend inversion |

---

## Why data-layer first (the case)

Four concrete defects in today's code make tasks **not worth building yet**:

1. **The DB cannot feed the LLM.** `inbox_messages.body_preview` is 150 chars
   (`server/app/gmail/parser.py:109`). Every LLM path that needs real text —
   `_reclassify_all`, `_score_all` in `server/app/workers/tasks.py` — refetches
   every thread from Gmail **sequentially at ~200ms each**. A task engine that
   re-extracts state on correction or schema change would multiply this cost
   unboundedly. The system of record must hold full bodies.
2. **Full sync destroys identity.** `full_sync_inbox` calls
   `inbox_repo.clear_user_inbox` (wipe + repopulate). Today that only loses
   `bucket_id` (recomputed). Once `task_links` / `task_events` FK onto
   `inbox_threads.id`, any `HistoryGoneError` would cascade-orphan a task's
   entire evidence trail. **Wipe-based full sync is incompatible with tasks**
   and must become reconciliation before task tables exist.
3. **Sync only sees additions.** `fetch_history_records` uses
   `historyTypes=["messageAdded"]`. Archives, deletions, read-state are
   invisible; the backend drifts silently. A HUD claiming "here is your task
   state" cannot be built on a copy known to drift.
4. **Push is lossy with no replay.** `PubSubDispatcher` drops frames on full
   queues; `_publish` logs `subscribers=0` and the frame is gone. The client
   compensates with watchdog timers (`Home.tsx` 60s/150s, `useInbox`
   `EXTEND_TIMEOUT_MS`). Task-state transitions are higher-stakes than inbox
   rows: a dropped frame means the HUD silently lies. Events need an id'd,
   replayable log.

## The guarantees (what the data layer promises the task engine)

- **G1 — Freshness:** new mail in Gmail is in Postgres, classified, and pushed
  to a connected HUD within **≤5s** (push path), with a ≤90s reconciliation
  bound when push fails (demoted poll). Inactive users converge within 1h.
- **G2 — Completeness:** archive / delete / read-state changes in Gmail are
  mirrored (rows marked, never silently divergent); a daily drift sweep bounds
  residual divergence.
- **G3 — Durability of identity:** an `inbox_threads.id` is stable for the
  life of the thread; no sync path deletes rows that task data references.
  Full sync = reconcile, not wipe.
- **G4 — Self-sufficiency:** the DB holds full message text; no steady-state
  LLM or UI path needs a Gmail round-trip.
- **G5 — Queryability:** arbitrary text search over subject/sender/body
  returns in <100ms at single-user inbox scale (10⁴–10⁵ messages) — the HUD
  EDA loop budget.
- **G6 — Ordered, resumable events:** every backend state change appends to a
  per-user ordered event log; SSE delivers with ids; a reconnecting client
  replays the gap or is told to snapshot. No silent loss.

Phases 0–3 establish G1–G6. The task engine (Phase 4+) consumes them.

## Core bets (decisions, committed)

| Question | Position |
|----------|----------|
| Faster Gmail sync | **Gmail `users.watch` push via Google Cloud Pub/Sub** (sub-second signal), with the existing 30s beat poll demoted to a 90s active-user reconciliation net + hourly all-user sweep. Polling alone cannot hit G1 without burning quota; we already live in a Google Cloud project (OAuth client), so the marginal infra is one Pub/Sub topic + one webhook route. |
| Storage/search | **Postgres-native**: full `body_text` on `inbox_messages` (TOAST handles size), generated `tsvector` column + GIN for FTS, `pg_trgm` GIN for sender/subject substring search. No Elasticsearch/Typesense — at this scale they are pure operational drag on Railway. |
| Event propagation | Keep SSE (not WebSocket). Add a **Redis Stream per user** (`events:{uid}`, `XADD MAXLEN ~1024`) as the durable event log; pub/sub stays as the wakeup signal; SSE frames carry stream ids in the `id:` field so `EventSource`'s automatic `Last-Event-ID` gives free resume. |
| Task state representation (VISION q-a) | **LLM-proposed from a natural-language goal, user-confirmed, constrained to a fixed meta-schema** (entities + one enum stage pipeline + typed attrs). Not freeform, not templates-only. |
| Email → state transition (VISION q-b) | A second LLM stage (extraction) separate from relevance; output validated mechanically (legal stage, evidence quote must substring-match the message, confidence threshold); sub-threshold results land in a **review queue** instead of mutating state. State is an **append-only event ledger** (`task_events`), so every transition is revertible. |
| Manual correction (VISION q-c) | First-class, not a settings page: attach/detach on every thread row, revert on every event, a per-task review queue; corrections append ledger events AND feed back into the task's criteria as tagged examples (reusing `bucket_repo.formulate_criteria`'s `<positive>/<nearmiss>` format). |
| Actions + consent (VISION q-d) | v1 = exactly two action kinds (Gmail **draft** creation, Gmail **label/archive**), three per-task consent levels (`off` / `propose` / `auto`), `auto` permitted only for reversible actions; sending mail is never `auto` in v1. Incremental OAuth re-consent (`gmail.modify`, `gmail.compose`) only when a user first enables actions. |
| Buckets → tasks | Buckets become `tasks` rows with `kind='bucket'` in **Phase 5**, after the task engine is proven; until then they coexist and `inbox_threads.bucket_id` keeps working. Single-pick bucket semantics are preserved as "highest-confidence bucket-kind match"; tracker tasks are many-to-many via `task_links`. |
| AgentMail (003 flows idea) | **Rejected for this repo's scope.** It replaces the sync problem with a mail-forwarding topology change and gives up the queryable local copy the HUD needs. Revisit only for owned-domain inboxes, post-HUD. |

## Phasing (each independently shippable)

| Phase | Ships | Guarantee | Rough scope |
|-------|-------|-----------|-------------|
| **0 — Bodies & self-sufficiency** | Migration: `body_text`, `labels`, `is_unread`, thread `last_activity_at`/`is_archived`; ingest stores full bodies; `_reclassify_all` + `_score_all` read from Postgres (delete the Gmail refetch loops); bounded thread-pool for Gmail `threads.get` | G4 | ~1 wk |
| **1 — Search** | Migration: `search_tsv` + trgm indexes; `GET /api/search`; search bar in the SPA | G5 | ~1 wk |
| **2 — Push sync & reconciliation** | `users.watch` + Pub/Sub topic + `POST /api/gmail/webhook`; watch-renewal beat; expanded `historyTypes` (archive/delete/labels mirrored); **full sync rewritten as reconcile (no wipe)**; beat demoted; hourly all-user sweep; drift sweep | G1, G2, G3 | ~2 wk |
| **3 — Event log & resume** | `realtime/event_log.py` (Redis Streams); SSE `id:`/`Last-Event-ID` resume; ingest/enrich event split (`thread_upserted` → `thread_enriched`); client watchdog timers deleted | G6 | ~1 wk |
| **4 — Task engine MVP** | `tasks`/`task_entities`/`task_links`/`task_events` tables; creation wizard (goal → LLM-proposed schema → confirm); relevance + extraction pipeline; HUD home route, task board, review queue, attach/detach | — | ~3–4 wk |
| **5 — Bucket unification** | Buckets migrated to `tasks(kind='bucket')`; `/api/buckets` aliased then removed; `inbox_threads.bucket_id` dropped in favor of links + denormalized `display_task_id` | — | ~1 wk |
| **6 — Actions & consent** | OAuth incremental scopes; draft + label actions; per-task policy; action runs in the ledger | — | ~2 wk |

Phases 0–3 are deliberately **before** the task engine: that ordering *is* the
lens. Phase 4 starts only when G1–G6 hold in prod.

## Constraints honored

- Stack unchanged: FastAPI + Celery + Postgres + Redis + React 19/Vite on
  Railway. The only new external touchpoint is a Google Cloud Pub/Sub topic in
  the GCP project that already hosts the OAuth client — justified in
  `agent-2-01-sync.md`.
- All migrations are Alembic (`server/migrations/versions/0006_*` onward).
- Beat remains single-replica; new beat entries documented per file.
