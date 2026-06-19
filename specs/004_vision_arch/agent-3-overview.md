<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 3 — task-engine first -->

# Agent 3 — Overview: Tasks Are the Product

> Lens: **task-engine first.** The task abstraction is designed in full first; the data
> layer, API, and HUD are derived backward from what the engine requires. Companion
> files: `agent-3-task-model.md` (the abstraction, pipeline, correction loop, actions),
> `agent-3-implementation.md` (schemas, migrations, API, frontend, phases in detail).

## Why task-first is the right order

VISION.md names three layers: tasks, the data layer under them, the HUD over them. The
temptation is to "fix the substrate first" (push-based sync, full-body storage, search)
because those are well-understood engineering problems. That order is wrong for this
codebase, for three reasons grounded in the actual code:

1. **The substrate's requirements are unknowable until the task model exists.** Today's
   substrate was shaped by buckets: `InboxMessage.body_preview` is 150 chars
   (`server/app/db/models.py:86`) because the inbox list view only renders a preview, and
   every LLM path that needs more (`_reclassify_all`, `_score_all` in
   `server/app/workers/tasks.py`) re-fetches full threads from Gmail at ~200ms each.
   Whether that's a bug or a fine trade-off depends entirely on how often the task engine
   re-reads bodies — which depends on the correction loop design. Optimizing sync latency
   from 30s to 2s before knowing whether task-state extraction is order-sensitive is
   guesswork.

2. **Buckets already prove the engine's hardest primitive.** The classify path
   (`server/app/llm/classify.py` → `classify_thread.parse_response` validating LLM output
   against a closed set) and the draft-preview wizard
   (`draft_preview_bucket` → `preview_cache` → SSE, with user-confirmed
   positives/near-misses folded into criteria via `bucket_repo.formulate_criteria`) are
   exactly the relevance half of a task and exactly the propose→preview→confirm UX a task
   wizard needs. The marginal cost of generalizing buckets into tasks *now* is low; the
   cost of building substrate features against the bucket shape and then re-fitting them
   to tasks is high.

3. **The riskiest open questions are task-model questions.** All four open questions in
   VISION.md (schema definition, email→transition mapping, correction UX, action consent)
   are about the task abstraction. If the schema language is wrong, no amount of sync
   freshness saves the product. Get the abstraction validated with the find-a-job toy on
   today's 30s poll; upgrade the substrate when a measured requirement forces it.

The plan therefore inverts the "infrastructure first" instinct: **Phase 0 does only the
substrate work the task engine provably cannot live without** (derived below), and
everything else ships behind the task model.

## Core bets (positions on VISION.md's open questions)

Full arguments in `agent-3-task-model.md`; the commitments:

| Open question | Position |
|---|---|
| **(a) How is a task's state representation defined?** | A constrained, typed schema language — the **Entity-Pipeline Schema (EPS)**: a task tracks a set of *entities*, each with typed *attributes* and a *stage* drawn from a finite pipeline + terminal states. The LLM **proposes** an EPS from the user's natural-language goal; the user **edits and confirms** it in the wizard (same propose→confirm shape as the bucket draft preview). Templates are just canned proposals; free-form JSON is rejected. Buckets are the degenerate case: `state_schema = NULL`. |
| **(b) How do emails map to state transitions, and how is that validated?** | A two-stage LLM pipeline: a per-thread **triage** call (extends today's classify: picks a bucket *and* flags relevant tasks) and a per-(thread, task) **extraction** call that proposes transitions as structured JSON. Validation is mechanical, server-side, non-LLM: Pydantic shape validation against the task's EPS, stage-legality checks, entity resolution, and a **verbatim-evidence requirement** (the quoted evidence must literally appear in the thread text — same trick `score_thread`'s `snippet` already uses). Invalid proposals are logged and surfaced, never applied. Applied transitions carry confidence and are user-reversible. |
| **(c) Manual correction UX/data flow?** | Correction is a first-class write path, not an afterthought: attach/detach threads (user links are sticky — the LLM cannot undo them), stage/attribute overrides, entity merge/rename. Every correction is an append-only `task_events` row with `actor='user'`; corrections are injected into future extraction prompts as feedback exemplars (the same `<positive>/<nearmiss>` mechanism `formulate_criteria` uses today). The task detail page is *built around* the review feed: every LLM-made change is visible, evidenced, and one-click reversible. |
| **(d) What actions may tasks take; consent model?** | A capability ladder, default-deny, per-task grants stored in `tasks.actions_policy`: (L0) internal state writes — no consent; (L1) Gmail label/archive — per-task toggle, requires the `gmail.modify` scope re-consent; (L2) outbound mail — **propose-only**: the task creates a Gmail *draft* and an approval card in the HUD; the user sends. No autonomous sending in any phase of this plan. Every action is an auditable `task_actions` row with a `proposed → approved → executed` lifecycle and a per-task pause switch. |

## What the task engine demands of the substrate (derived requirements)

Working backward from the engine in `agent-3-task-model.md`, these are the *only*
substrate changes the engine cannot ship without — each tied to a concrete defect in
current code:

1. **Persist full bodies.** Extraction, backfill (running a new task over existing
   threads), and correction-triggered re-extraction all need full thread text on demand.
   Today that means sequential `gmail.users().threads().get(format="full")` calls
   (`_reclassify_all`, `_score_all` in `workers/tasks.py`) — 200 threads ≈ 40s of Gmail
   round-trips *per re-run*, against quota. **Requirement:** add
   `InboxMessage.body_text TEXT` (parser already produces it: `ParsedMessage.body_text`
   in `server/app/gmail/parser.py` — it's computed and thrown away). Postgres TEXT is
   fine at this scale; no blob store yet (YAGNI).

2. **Full sync must stop destroying history.** `full_sync_inbox`
   (`server/app/workers/gmail_sync.py:204`) calls `inbox_repo.clear_user_inbox` and
   repopulates the newest 200 threads. Any thread older than the newest 200 — including
   threads pulled in by `extend_inbox_history` and **threads attached to tasks** — is
   deleted whenever a history cursor expires (404 → full sync fallback in
   `poll_new_messages`). For buckets that was a tolerable reset; for tasks it orphans
   `task_threads` links and evidence. **Requirement:** full sync becomes a reconciling
   upsert (no wipe), and task evidence is additionally denormalized into `task_events`
   (verbatim quote + gmail ids) so audit survives any storage churn.

3. **Tasks run while the user is away.** Polling is gated on the `active_users` zset
   (`enqueue_polls` in `workers/tasks.py`) — no SSE connection, no sync. A job pipeline
   must advance overnight. **Requirement:** a second beat entry polls users who own
   active tracker tasks hourly regardless of SSE presence (sharded across the hour).
   The 30s active-user cadence is otherwise *sufficient* for v1 trackers: state
   transitions are minutes-scale human processes. Gmail push (`users.watch` + Pub/Sub)
   is scheduled as a later substrate upgrade, not a prerequisite.

4. **Searchable storage.** The HUD's EDA loop and the "search-to-attach" correction flow
   need text search. **Requirement:** Postgres FTS — a generated `tsvector` column over
   subject + body_text with a GIN index, exposed as `GET /api/search`. At hundreds-to-
   thousands of threads per user this is comfortably sufficient; pgvector/semantic search
   is explicitly deferred until FTS measurably fails.

5. **Realtime: keep, harden slightly.** The existing Redis pubsub → `PubSubDispatcher`
   → SSE chain (`server/app/realtime/pubsub.py`, `server/app/api/sse.py`) is adequate.
   Its known weakness — fire-and-forget delivery loss, currently patched by the
   reclassify watchdog `setTimeout`s in `client/src/pages/Home.tsx` — is fixed for tasks
   with **versioned state**: `tasks.version` increments on every event; SSE
   `task_updated` frames carry `{task_id, version}`; the client refetches the task
   snapshot on any version gap. No new transport.

Explicitly rejected: the agentmail migration floated in `specs/003_task_hud/flows.md`.
It replaces a working readonly Gmail-history sync with mail forwarding through a third
party — losing history fidelity, adding a vendor in the trust path for *all* mail, and
violating the evolve-the-stack constraint. The action layer it promises is delivered
instead by L1/L2 capabilities on the existing Gmail API surface.

## Buckets unification (summary; mechanics in agent-3-implementation.md)

Buckets become rows in `tasks` with `kind='bucket'`, `state_schema=NULL`, and
`exclusive_group='inbox'` (the property that exactly one bucket-kind task claims a
thread — preserving today's single `bucket_id` semantics inside the general multi-task
relevance model). Migration `0006_tasks` copies every `Bucket` row into `tasks`
**reusing the same id** so `inbox_threads.bucket_id` stays valid during transition, and
materializes existing assignments as `task_threads` rows. `/api/buckets` becomes a thin
shim over the task repo until the SPA is HUD-first, then is removed along with the
`buckets` table (migration `000N_drop_buckets`, final phase). `bucket_repo.formulate_criteria`
is lifted verbatim into the task repo — it is already the relevance-criteria compiler.

## Phasing (summary; scope detail in agent-3-implementation.md)

| Phase | Ships | Independent value |
|---|---|---|
| **0 — Substrate floor** | `body_text` column + backfill-on-touch, non-destructive full sync, FTS + `/api/search` | Reclassify/preview stop hammering Gmail; inbox gets a search box |
| **1 — Task model under buckets** | `tasks`/`task_threads` tables, migration 0006, task repo, triage prompt replaces classify prompt, `/api/buckets` shimmed | Identical UX on the new engine; zero-regression cutover |
| **2 — Trackers MVP** | EPS schema language, task wizard (goal → proposed schema → relevance preview → backfill), extraction pipeline, entities/events, `/tasks/:id` page with pipeline board + corrections | Find-a-job works end to end |
| **3 — HUD inversion** | HUD becomes `/`, inbox demoted to `/inbox`, review feed, recent-activity ticker, offline hourly polling for task owners | The product becomes the HUD |
| **4 — Actions** | `task_actions` ledger, approval queue, `gmail.modify` re-consent flow, label/archive (L1) + draft-reply (L2) | Tasks act, user consents |
| **5 — Substrate on demand** | Gmail watch/push, pgvector, blob bodies | Only when a phase-2/3/4 metric demands it |
