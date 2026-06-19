<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 1 — incremental evolution -->

# Vision Architecture — Plan Overview (Agent 1: Incremental Evolution)

Lens: morph the existing codebase into the VISION.md destination with the smallest
safe steps. Every phase ships something a user can touch; nothing working is
rewritten when it can be extended. The existing stack — FastAPI + Celery + Postgres
+ Redis + React 19/Vite on Railway — is kept wholesale. The bucket classifier, the
sync engine (`server/app/workers/gmail_sync.py`), the pubsub→SSE realtime path, and
the draft-preview wizard pattern are all treated as **v1 scaffolding of the task
engine**, not legacy to replace.

Companion files:

| File | Contents |
|------|----------|
| `agent-1-data-model.md` | New SQLAlchemy models, Alembic migration sketches, body storage + Postgres FTS |
| `agent-1-task-engine.md` | Relevance → extraction → validation pipeline, positions on all four open questions, bucket unification, actions + consent |
| `agent-1-sync-api-frontend.md` | Gmail push sync, label/delete mirroring, full API surface, SSE events, HUD-first UI inversion |

---

## Core bets (answers to VISION.md's open questions)

1. **Task state representation — LLM-proposed, user-edited, from a constrained
   schema vocabulary.** The user types a natural-language goal ("find a job"); a
   worker task proposes (a) relevance criteria in the exact shape
   `bucket_repo.formulate_criteria` already produces and (b) a **state schema**
   limited to a small typed vocabulary: an optional `entity_by` grouping key plus
   fields of kind `pipeline` (ordered stages), `date`, `text`, `number`, `flag`.
   The user reviews/edits in a wizard that reuses the `NewBucketModal`
   form→pending→review pattern. No free-form JSON Schema: the constraint is what
   makes transitions mechanically validatable and the board mechanically
   renderable. Templates are deferred — they fall out later as saved schemas.

2. **Emails → state transitions via a second, separate LLM stage with mechanical
   validation.** Relevance (stage 1) reuses the classify mechanism. Extraction
   (stage 2) is a per-(task, thread) call that sees the state schema + the task's
   current entity registry and must emit a constrained JSON transition
   `{entity_key, field, new_value, evidence_quote, confidence}`. Validation is
   code, not vibes: type/enum membership, entity resolution against known keys,
   idempotency via a unique constraint on `(task_id, message_id, field)`, and a
   confidence gate. Low-confidence or schema-invalid output lands as
   `pending_review`, never applied. State is a fold over an append-only
   `task_events` log — which makes correction and revert first-class.

3. **Manual correction is the product surface, not an afterthought.** The task
   detail page is built around the event log: every entity state shows the event
   (and quoted evidence) that produced it; users attach/detach threads
   (user-origin link rows the LLM can never override), approve/reject pending
   events, edit entity state directly (a user-origin event), and revert applied
   events (re-fold). Corrections feed back as `<positive>`/`<nearmiss>` examples
   appended to the task's criteria — the same few-shot mechanism buckets already
   use, so accuracy improves without any new ML machinery.

4. **Actions: a capability ladder with per-task, per-action-type consent.**
   `action_mode` per task: `off` (default) → `propose` (every action is a pending
   row the user approves) → `auto` (allowlisted reversible action types only:
   archive, label; never send). Drafting replies is propose-only; **sending is
   always an explicit user click** in every mode in this plan's horizon. Action
   capability requires a Gmail scope upgrade (`gmail.readonly` →
   `gmail.modify`/`gmail.compose`) via an incremental re-consent OAuth flow —
   read-only users are never asked for write scopes.

5. **Buckets unify into tasks by ID-preserving migration, not rewrite.** A
   migration converts each `buckets` row into a `tasks` row **with the same
   primary key** and `kind='classify'`, so `inbox_threads.bucket_id` re-targets
   its FK without repointing a single row. `/api/buckets` becomes a thin shim over
   tasks for one release, then dies. `reclassify_user_inbox` becomes
   `backfill_task`.

6. **Sync gets faster by adding a trigger, not a new engine.** Gmail
   `users.watch` → Cloud Pub/Sub → `POST /api/gmail/push` webhook, which simply
   enqueues the existing `poll_new_messages` (debounced). The 30s beat poll
   remains as the reconciliation fallback (relaxed to 120s for push-healthy
   users). History sync widens from `messageAdded` to include
   `labelRemoved`/`messageDeleted` so archives and deletions stop drifting.

7. **Searchable storage = Postgres, no new datastore.** Store full
   `body_text` (today only a 200-char `body_preview` exists), add a generated
   `tsvector` + GIN index, expose `GET /api/search`. This also deletes the
   single biggest performance wart in the codebase: `_reclassify_all` and
   `_score_all` in `server/app/workers/tasks.py` re-fetch every thread body from
   Gmail at ~200ms each because Postgres doesn't have the bodies. After Phase 0
   they read Postgres. Vector search is an explicitly deferred option.

## Stack changes proposed

- **No replacements.** FastAPI, Celery, Postgres, Redis, React/Vite, Railway all stay.
- **Additions:** Google Cloud Pub/Sub topic + subscription (hard requirement of
  Gmail push notifications — there is no webhook-direct option), `react-router-dom`
  in the client (the SPA currently has no router; HUD-first needs URLs), Postgres
  FTS (built-in, zero infra).
- **Explicitly rejected:** AgentMail migration (floated in
  `specs/003_task_hud/flows.md`). It replaces the entire Gmail sync core — the
  opposite of incremental — and forfeits the history-cursor machinery that already
  works. Revisit only for the future "domains you own" data source.
  Elasticsearch/Typesense rejected: per-user corpus is 10²–10⁴ threads; Postgres
  FTS is sufficient and adds zero services.

## Phasing (each independently shippable)

| Phase | Ships | Rough scope |
|-------|-------|-------------|
| **0 — Data foundations** | Full bodies in Postgres (`inbox_messages.body_text`), FTS + `GET /api/search`, search box in current inbox UI; reclassify/preview read Postgres instead of re-fetching Gmail | ~3–4 days. Migration 0006. |
| **1 — Routing + HUD shell** | `react-router-dom`; `/inbox` = today's UI verbatim; `/` = HUD skeleton (sync recency strip, bucket summary cards, global search) | ~2–3 days. No backend change. |
| **2 — Sync upgrade** | Gmail `users.watch` push → webhook → debounced poll; label/delete mirroring (`is_archived`, hard-delete); beat relaxed to reconciliation role | ~4–5 days. Migration 0007 + GCP setup. |
| **3 — Tasks v1 (track)** | `tasks`/`task_thread_links`/`task_state_entities`/`task_events` tables; creation wizard (goal → proposed criteria+schema → review); relevance + extraction pipeline in worker; task board + event feed pages; full manual-correction loop | ~10–14 days. Migration 0008. The big one — see `agent-1-task-engine.md` for internal slicing. |
| **4 — Bucket unification** | Buckets migrated to `kind='classify'` tasks (ID-preserving); `/api/buckets` shim; inbox pills read tasks; bucket-specific code deleted | ~3–4 days. Migration 0009. |
| **5 — Actions** | OAuth scope-upgrade flow; `task_actions` + grants; propose-mode archive/label/draft-reply; auto-mode for archive/label | ~5–7 days. Migration 0010. |
| **6 — Optional later** | pgvector semantic search; saved-schema templates; multi-source HUD groundwork | unscoped |

Dependency notes: 3 depends on 0 (extraction reads bodies from Postgres);
2 is independent of 0/1 and can run in parallel; 4 depends on 3; 5 depends on 3
(not 4). The earliest user-visible "vision moment" — creating *find a job* and
watching a pipeline build itself — lands at the end of Phase 3.
