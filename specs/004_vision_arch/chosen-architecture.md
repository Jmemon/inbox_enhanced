<!-- stamp: 5ecf783 (feature/phase1-routing-shell) | 2026-07-06 | Chosen architecture — synthesis of agents 1–3 -->

# 004 — Chosen Architecture

Synthesis of the three competing proposals (`agent-1-*` incremental evolution,
`agent-2-*` data-layer first, `agent-3-*` task-engine first) into one committed
design. Every contested axis below was decided explicitly with the user on
2026-07-05; unanimous positions were adopted as-is. This file is the canonical
record — the agent files remain as rationale archives.

**Lens: Agent 3's ordering (task-engine first, substrate on demand), Agent 1's
incremental mechanics, Agent 2's data guarantees where they gate tasks.** The
30s poll remains the sync engine for now; the risky, product-defining work —
the task engine — ships first on a minimal-but-safe data floor.

---

## 1. Unanimous positions (adopted without contest)

1. **Stack unchanged**: FastAPI + Celery + Postgres + Redis + React 19/Vite on
   Railway. AgentMail rejected for this repo's scope (revisit as the separate
   Intent Gateway component, `specs/later/scratch.md`). No external search
   engine.
2. **Full message bodies in Postgres + Postgres FTS** (generated `tsvector` +
   GIN, `pg_trgm` for fuzzy sender/subject) + `GET /api/search`. Kills the
   reclassify/preview Gmail-refetch loops in `workers/tasks.py`.
3. **SSE stays** — no WebSocket. Client→server needs are plain POSTs.
4. **State representation** (VISION q-a): LLM-proposed from a natural-language
   goal, user-edited/confirmed in a wizard reusing the bucket draft-preview
   pattern, constrained to a typed meta-schema. No free-form JSON. Templates
   deferred (they are just saved schemas).
5. **Emails → transitions** (q-b): a second extraction LLM stage, separate from
   relevance, mechanically validated in code before any DB write; append-only
   `task_events` ledger makes every write revertible.
6. **Manual correction** (q-c): first-class product surface — sticky user
   attach/detach the LLM can never override, one-click revert with evidence
   quotes, corrections feed the task's criteria as `<positive>`/`<nearmiss>`
   examples via the existing `bucket_repo.formulate_criteria` grammar.
7. **Actions** (q-d): default-deny capability ladder per task, incremental
   OAuth re-consent (`gmail.readonly` → `gmail.modify`/`gmail.compose`),
   reversible actions (archive/label) may auto-run under explicit grants,
   outbound mail is draft-only. **No autonomous send, ever, in this horizon.**
8. **Buckets are the degenerate task**: unified via an ID-preserving migration
   (`kind='bucket'`), `/api/buckets` shimmed one release, then deleted.
9. **HUD inversion** via `react-router-dom`: `/` = HUD, `/inbox` = today's UI
   demoted, `/tasks/:id` = pipeline board + review feed + threads panel.

Two mandatory fixes folded in (flagged during synthesis):

- **Full sync must stop wiping.** `full_sync_inbox`'s wipe-and-repopulate
  (via `inbox_repo.clear_user_inbox`) would orphan task links/evidence whenever
  a history cursor expires. It becomes a reconciling upsert **before** any task
  table exists. (Agents 2 & 3 both required this; agent 1 missed it.)
- **LLM observability was under-specced by all three.** VISION mandates
  per-call metrics *persisted, not logged*. An `llm_calls` table +
  instrumentation at the `llm/client.py` choke point is part of Phase 0.

## 2. Decided axes (with the decision)

| # | Axis | Decision |
|---|------|----------|
| D1 | Gmail push sync timing | **Deferred until after tasks** (Phase 6). Push's agreed shape — `users.watch` + Pub/Sub doorbell that just enqueues the existing `poll_new_messages` — is unchanged whenever it lands. Interim: 30s poll for active users + hourly beat poll for tracker-owners so tasks advance offline. |
| D2 | Bucket unification path | **One triage call now, tables later.** From Phase 2, a single LLM call per thread returns `{bucket pick + relevant trackers}` (no doubled classify cost), dual-writing `inbox_threads.bucket_id` + `task_thread_links`. The `buckets` table survives untouched until trackers are proven (Phase 4), then folds in via agent 1's ID-preserving migration. |
| D3 | Transition apply policy | **Confidence-gated hybrid + fences.** High-confidence transitions auto-apply (revert affordance + evidence in the feed); low-confidence → pending-review tray; LLM backward pipeline moves always → review. Correction fences: after a user corrects an entity, only evidence from a *newer* message can move it. |
| D4 | Realtime hardening | **Task versioning only.** `tasks.version` increments per applied event; SSE `task_updated {task_id, version}`; client refetches on gap. Inbox keeps existing watchdogs. Redis Streams / `Last-Event-ID` resume deferred. |
| D5 | Revert mechanics | **Recompute from remaining events.** Revert marks the event `reverted`, then refolds that entity's state from its remaining applied events (per-entity refold, not global replay). No compensating events. |
| D6 | Extraction model tier | **Sonnet-class for extraction** (new `LLM_EXTRACT_MODEL`, default `anthropic/claude-sonnet-4.5` via OpenRouter). Triage stays Haiku-class. Extraction mutates user-visible state and volume is tiny. |
| D7 | Gmail mirror scope | **Archive + soft-delete + unread.** `is_archived` on threads; message deletions are *soft* (`is_deleted` — task evidence must survive); read/unread mirrored; label snapshot stored (JSONB) but not interpreted. Belt: `task_events` denormalizes `gmail_message_id` + verbatim quote so audit survives any churn. Mirroring needs only widened `historyTypes` — it works with polling, no push dependency. |
| D8 | LLM observability timing | **Phase 0, before the engine.** Baseline data from the first call; task unit economics measurable from day one. |

## 3. Phasing

Each phase independently shippable; Phase 2 is the big one.

| Phase | Ships |
|-------|-------|
| **0 — Data floor** | `inbox_messages.body_text` persisted (forward-fill on sync touch); full sync → reconciling upsert (no wipe; `clear_user_inbox` survives only for account deletion); widened `historyTypes` → archive/soft-delete/unread mirroring; FTS (`search_tsv` generated columns + GIN, `pg_trgm`) + `GET /api/search` + search box in the inbox UI; `_reclassify_all`/`_score_all` read Postgres; `llm_calls` table + client instrumentation. Migration 0006. |
| **1 — Routing shell** | `react-router-dom` (bun); `/` = HUD skeleton (global search, sync-recency strip, bucket summary cards); `/inbox` = today's UI verbatim; `AppShell` nav; SSE singleton moves up so navigation doesn't drop `active_users` registration. Backend: only the `last_sync` Redis marker + `GET /api/sync/status` for the freshness strip (per §6). |
| **2 — Trackers MVP** | Task tables (migration 0007): `tasks`, `task_thread_links`, `task_state_entities`, `task_events`. EPS schema language + validator. Creation wizard (goal → proposed schema+criteria → edit/confirm → backfill). Single triage call replaces classify (dual-write). Extraction pipeline (decoupled Celery task) + mechanical validator + confidence gate. Task board, review tray, full correction loop. `tasks.version` SSE. Backfill with FTS prefilter, chronological extraction, SSE progress. |
| **3 — HUD inversion** | HUD becomes the product surface: task card grid (stage histograms, pending-review badges), aggregated review tray, activity ticker. Inbox demoted to nav link. Hourly beat poll for owners of active trackers (sharded over the hour; beat stays single-replica). |
| **4 — Bucket unification** | ID-preserving migration: `buckets` rows → `tasks(kind='bucket')` with same PKs; `inbox_threads.bucket_id` FK retargets with zero row updates; `/api/buckets` shim one release; bucket modals/`reclassify_user_inbox`/`preview_cache` bucket paths deleted; wizard with schema step skipped covers bucket creation. |
| **5 — Actions** | Migration: `task_actions` + `task_action_grants` + `users.gmail_granted_scopes`. OAuth incremental re-consent flow. Per-task `action_mode` dial `off`/`propose`/`auto`. Vocabulary: `archive_thread`, `label_thread` (auto-eligible, reversible, undo affordance), `draft_reply` (propose-only). Every action row carries `source_event_id` — audit chain: email → event (evidence) → action. |
| **6 — On demand** | Gmail push (`users.watch` + Pub/Sub + OIDC-verified webhook + watch-renewal beat; poll demoted to reconciliation), Redis Streams SSE resume, pgvector semantic search, schema templates — each gated on a measured need, not scheduled. |

The earliest vision moment — create *find a job*, watch the pipeline build
itself — lands at the end of Phase 2.

## 4. Task engine core

New module `server/app/task_engine/` (not `app/tasks/` — avoids collision with
`workers/tasks.py`): `schema.py` (EPS pydantic models), `repo.py` (never
commits; same contract as `inbox_repo`), `criteria.py`
(`formulate_criteria` moved verbatim), `transitions.py` (pure validator),
`engine.py` (orchestration). Prompts in `llm/prompts/`: `triage_thread.py`,
`extract_transition.py`, `propose_task.py`; `score_thread.py` reused unchanged.

### 4.1 Definition

`Task = (goal, relevance_criteria, state_schema?, action_policy, status)`.
`state_schema = NULL` **is** a bucket. `kind ∈ {bucket, tracker}` — bucket-kind
keeps today's exactly-one-per-thread semantics (single pick keyed off kind
directly; no `exclusive_group` generality); trackers are many-to-many via
`task_thread_links`. `relevance_criteria` uses the exact grammar
`bucket_repo.formulate_criteria` produces today.

### 4.2 EPS (Entity-Pipeline Schema), v1

```jsonc
{
  "version": 1,
  "entity": {            // null → singleton task (one implicit entity)
    "noun": "company",
    "identity_hint": "the company (or its agency) the email is from or about",
    "attributes": [      // types: string | number | datetime | boolean | enum
      {"key": "role", "type": "string"},
      {"key": "next_event_at", "type": "datetime"}
    ]
  },
  "pipeline": {
    "stages":   ["applied", "recruiter_screen", "interview", "onsite", "offer"],
    "terminal": ["offer_accepted", "rejected", "withdrawn"]
  }
}
```

One entity type, one pipeline, typed attrs — small enough to be validatable,
renderable (board columns are generated), and promptable. No
`allow_skip`/`allow_regress` flags; fixed rule instead: forward/lateral/skip
apply freely, **LLM backward moves always go to review** (real life regresses;
a human confirms it). Post-creation edits are additive-only (new stage /
attribute / terminal); destructive edits = archive + recreate (backfill makes
recreation cheap). Multi-entity schemas are a future `version: 2`.

### 4.3 Pipeline

- **Triage** runs where classify runs today (inside sync,
  `gmail_sync._classify_batch` → `_triage_batch`): one Haiku-class call per
  thread against all active tasks →
  `{"bucket_name": ..., "relevant_tasks": [{name, confidence}]}`. Writes
  `bucket_id` (unchanged semantics) + `task_thread_links(origin='llm')`,
  skipping any link whose `origin='user'` (sticky).
- **Extraction** is a **decoupled follow-up Celery task** per (tracker,
  thread): Sonnet-class, does not hold `sync_lock` (sync latency stays flat;
  idempotency comes from constraints, not locks). Prompt inputs: goal + EPS +
  current entity roster (match before create) + recent user-correction
  exemplars + thread text from Postgres with per-message id markers. Output:
  entity match/create, transition, attribute updates, verbatim evidence quote,
  confidence, or explicit no-op.
- Messages processed in `gmail_internal_date` order; one extraction at a time
  per (task, entity) — out-of-order transitions prevented structurally.

### 4.4 Validator (`transitions.py`, pure code, in order)

1. **Shape** — pydantic parse against the EPS; unknown stage/attribute or
   value that doesn't coerce → rejected (logged as
   `rejected_invalid` event, surfaced in the review feed — prompt-rot
   detector — never applied).
2. **Entity resolution** — normalized (casefold, strip punctuation) match
   against existing keys; `pg_trgm` similarity ≥ 0.6 counts as a match,
   0.4–0.6 → review instead of minting a duplicate.
3. **Stage legality** — terminal entities move only by user action; backward
   LLM moves → review.
4. **Evidence** — the quote must appear **verbatim** in the thread text
   (whitespace-normalized substring). No quote, no write — the cheapest
   effective hallucination guard.
5. **Correction fences** — a proposal touching a user-corrected entity is
   rejected unless its evidence message is newer than the correction.
6. **No-op filter** — `new_value == current` → skip.
7. **Idempotency** — insert guarded by partial unique index
   `(task_id, message_id, field)`; conflict → skip (re-sync replay).
8. **Confidence gate** — confidence is 0–100; ≥ 75 → applied (revert
   affordance); below → `pending_review` (stored, state untouched). Threshold
   is a setting (`TASK_APPLY_CONFIDENCE`, default 75).

### 4.5 Ledger + projection

- `task_events`: append-only; every row carries origin (`llm|user|system`),
  field-grained `old_value`/`new_value`, `evidence_quote`, confidence, soft
  `message_id` **plus denormalized `gmail_message_id`** (audit survives inbox
  churn), status (`applied|pending_review|rejected|reverted`).
- `task_state_entities`: the materialized board —
  `{entity_key, display_name, state JSONB}`; always derivable as a fold over
  applied events.
- **Revert = mark reverted + refold that entity** from its remaining applied
  events (`repo.refold_entity`; user-origin events win ties). Detach reverts
  all events sourced from that thread, then refolds touched entities.
- Every applied event bumps `tasks.version` (the SSE gap-detection counter).

### 4.6 Corrections

Sticky user links (attach = immediate extraction kick; detach = durable
negative the LLM can never re-attach), fences, drag-between-columns = manual
state edit (user-origin applied event), entity merge (events repointed, target
refolded), approve/reject in the review tray. Every correction optionally
appends a `<positive>`/`<nearmiss>` example to `tasks.criteria` — capped ~30
examples FIFO to bound prompt size. All correction endpoints are synchronous
FastAPI writes followed by a `task_updated` publish.

### 4.7 Backfill

On task creation: **FTS keyword prefilter** (the propose call also returns
5–10 search probes; never LLM-score the whole inbox), triage over candidates,
then extraction over matches in **ascending `gmail_internal_date` order** so
pipelines replay history correctly. Progress published every ~50 threads
(`task_backfill_progress`); wizard shows a live fill-up. Same lock/retry
discipline as `reclassify_user_inbox` (which Phase 4 deletes in favor of this).

## 5. Data layer

### 5.1 Guarantees the engine relies on

- **Identity durability**: no sync path deletes rows tasks reference. Full
  sync reconciles (upsert + mark-archived-if-absent), never wipes; message
  deletes are soft.
- **Self-sufficiency**: no steady-state LLM or UI path needs a Gmail
  round-trip; bodies live in Postgres (TOAST handles size; cap body
  contribution to tsvector defensively).
- **Freshness (interim)**: ≤35s for active users (existing poll), ≤1h for
  offline tracker-owners (Phase 3 beat entry). Upgraded to ≤5s only when
  Phase 6 push lands.

### 5.2 Schema deltas (sketch — final columns in the implementation plan)

- Migration 0006 (Phase 0): `inbox_messages.body_text`, `subject_cache`,
  `labels JSONB`, `is_unread`, `is_deleted`, `search_tsv` (generated) + GIN;
  `inbox_threads.is_archived`, `last_activity_at` (kills the recent-message
  outerjoin sort), subject tsvector/trgm indexes; `llm_calls` table.
- Migration 0007 (Phase 2): `tasks`, `task_thread_links`, `task_state_entities`,
  `task_events` — agent 1's four-table model with agent 3's denormalized
  `gmail_message_id` and `kind ∈ {bucket, tracker}` naming.
- Migration 0008 (Phase 4): bucket unification (ID-preserving copy, FK
  retarget, drop `buckets`).
- Migration 0009 (Phase 5): `task_actions`, `task_action_grants`,
  `users.gmail_granted_scopes`.

### 5.3 `llm_calls` (Phase 0)

One row per LLM call, written by `llm/client.py` (single choke point):
`model`, `stage` (`triage|extract|propose|score`), `task_id?`, `user_id`,
input/output/cache-read/cache-write tokens, derived `cost`, `ttft_ms` (where
streamed; else null), `duration_ms`, `outcome` (`success|retry|error`).
Rollups (per task / per user / system) are queries, not new infrastructure.

## 6. API & frontend

API surface and frontend structure follow agent 1's
`agent-1-sync-api-frontend.md` §3–4 with these substitutions: no
`/api/gmail/push` until Phase 6; `GET /api/sync/status` (agent 2) ships in
Phase 1 to feed the HUD freshness strip; task routes as specced (draft →
wizard → CRUD → board/events/threads → corrections → actions). SSE event
vocabulary: existing three + `task_draft_ready`, `task_updated
{task_id, version}`, `task_backfill_progress`. Frontend: agent 3's
`pages/hud/` + `pages/task/` component map (`PipelineBoard`, `EntityDrawer`,
`ReviewFeed`, `ThreadsPanel`, `SchemaEditor`, `NewTaskWizard` cloned from
`NewBucketModal`'s form/pending/review + SSE-or-poll idempotency pattern).
`useInbox`/`useInboxSse` internals survive untouched; inbox watchdogs retire
only when their producers do. Client state that must survive route
navigation lives in AppShell-level providers (`InboxProvider`; Phase 2 adds
a task-store provider alongside it) — pages consume stores, they don't own
them.

## 7. Deferred / rejected

**Deferred (gated on measured need):** Gmail push + Pub/Sub, Redis Streams +
`Last-Event-ID` resume, ingest/enrich split (classify stays inline while poll
is the trigger), pgvector, schema templates, multi-entity EPS, attachment
metadata/proxy, read-state write-through to Gmail.

**Rejected:** AgentMail as sync replacement (lives on only as the future
Intent Gateway ingress, `specs/later/scratch.md`); Elasticsearch/Typesense;
WebSocket; free-form JSON task state; fully user-authored schemas;
autonomous email sending.

## 8. Testing & ops invariants

- New repos follow the never-commit contract; Celery paths honor
  `CELERY_TASK_ALWAYS_EAGER=1` + fakeredis test seams.
- `transitions.py` is pure (no IO) — exhaustive unit tests: illegal stage,
  unknown attribute, fabricated evidence, fence violations, terminal locks,
  idempotent replay.
- Ship gate for Phase 2: the agent-3 worked example (`agent-3-task-model.md`
  §6) passes end to end against a real Gmail account, including both
  corrections.
- Beat remains single-replica (`railway.beat.toml`); new entries: hourly
  tracker-owner poll (Phase 3), watch renewal (Phase 6).
- No new Railway services in any phase.
