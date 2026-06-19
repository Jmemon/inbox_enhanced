<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 1 — incremental evolution -->

# Task Engine

The bucket classifier is v1 of this engine. Every piece below is an extension of
a mechanism that already exists in the repo: the criteria grammar
(`bucket_repo.formulate_criteria`), the one-call-per-thread parallel classify
(`llm/classify.py` under the shared `llm/client.py` semaphore), the
draft→preview→confirm wizard (`draft_preview_bucket` + `preview_cache` +
`NewBucketModal`), and the publish-after-commit SSE contract
(`workers/tasks.py::_publish`).

New code lives in:

- `server/app/tasks_engine/` — `task_repo.py`, `schema.py` (state-schema
  validation), `transitions.py` (mechanical validation + fold), `actions.py` (Phase 5)
- `server/app/llm/prompts/propose_task.py`, `relevance.py` (evolved
  `classify_thread.py`), `extract_transition.py`, `propose_action.py`
- `server/app/workers/task_engine_tasks.py` — Celery tasks
- `server/app/api/tasks.py` — HTTP router

---

## 1. Open question (a): how a user defines a task's state representation

**Position: LLM-proposed from a natural-language goal, constrained to a typed
vocabulary, user-edited before activation. Not fully user-authored (too much
friction), not templates (premature — templates are just saved schemas later).**

### The schema vocabulary (`tasks.state_schema` JSONB)

```jsonc
{
  "entity_by": "company",          // or null → singleton task
  "fields": [
    {"name": "stage", "kind": "pipeline",
     "stages": ["applied", "phone screen", "onsite", "offer"],
     "terminal": ["offer", "rejected", "withdrawn"]},
    {"name": "next_step_date", "kind": "date"},
    {"name": "recruiter", "kind": "text"},
    {"name": "referral", "kind": "flag"}
  ]
}
```

Five field kinds only: `pipeline` (ordered stages + terminal set), `date`,
`text`, `number`, `flag`. `server/app/tasks_engine/schema.py` owns
`validate_schema(dict) -> StateSchema` (pydantic model) and rejects anything
outside the vocabulary — this is the contract that makes both the transition
validator and the board UI mechanical. "Find a job" → entity_by=company +
pipeline; "find an apartment" → entity_by=listing + pipeline; a singleton
"track my visa application" → entity_by=null.

### Creation flow (mirrors the bucket draft-preview flow exactly)

1. `POST /api/tasks/draft {goal}` → 202 `{draft_id}`; API calls
   `draft_cache.mark_pending` (a generalization of
   `inbox/preview_cache.py` — same redis `SET ... EX 600`, key
   `task_draft:{draft_id}`) then enqueues Celery `propose_task_draft`.
2. Worker `propose_task_draft(user_id, draft_id, goal)`:
   - one LLM call (`prompts/propose_task.py`) returns
     `{name, criteria_description, state_schema}`; `schema.validate_schema`
     gates it; on validation failure retry once with the error appended, then
     fall back to `{entity_by: null, fields:[{"name":"status","kind":"text"}]}`.
   - reuse `_score_all` from `workers/tasks.py` (reading Postgres bodies after
     Phase 0) to surface example positives/near-misses for the criteria, same
     thresholds (`POSITIVE_THRESHOLD=7`, `NEAR_MISS_LOW/HIGH=4/6`).
   - cache-before-publish, then `_publish(uid, "task_draft_ready", {draft_id, ...})`.
3. Wizard review step renders: editable name, criteria description, the example
   confirm/reject list (identical interaction to `NewBucketModal`'s review
   step), and a **schema editor**: pipeline stages as a reorderable chip list,
   add/remove fields from the five kinds, rename `entity_by`.
4. `POST /api/tasks {name, goal, criteria_description, confirmed_positives,
   confirmed_negatives, state_schema}` → `bucket_repo.formulate_criteria`
   (unchanged function — same example grammar) builds `tasks.criteria`; row
   inserted; Celery `backfill_task(task_id)` enqueued (§3).

Schema edits after creation: `PATCH /api/tasks/{id}` accepts `state_schema`
with additive-only changes (add stage/field, rename via alias map) applied
freely; destructive changes (remove stage that has entities in it) are rejected
with 409 and the offending entity count — keep v1 simple.

---

## 2. Open question (b): how emails map to state transitions, and validation

**Position: two LLM stages with a mechanical validator between LLM output and
the database. Relevance and extraction are separate calls — relevance is cheap
and runs on every new thread; extraction runs only on (task, thread) pairs that
passed relevance. All writes go through the append-only `task_events` log; a
confidence gate routes uncertain transitions to human review instead of
applying them.**

### Stage 0 — trigger

`gmail_sync.partial_sync_inbox` / `full_sync_inbox` / `extend_inbox_history`
already return touched internal thread ids. The sync Celery tasks
(`poll_new_messages`, `full_sync_inbox_task`, `extend_inbox_history_task`,
`reclassify_user_inbox` in `workers/tasks.py`) gain one line after their
existing `_publish_thread_ids(...)`:

```python
task_engine_tasks.process_task_updates.apply_async(args=[user_id, ids], countdown=0)
```

Decoupled as its own Celery task (not inline) so: (1) sync latency — and the
`sync_lock` hold time — doesn't grow with task count; (2) the task engine does
**not** need `sync_lock` at all (it never writes `inbox_*` tables; its
idempotency comes from `uq_task_thread` and the partial unique index
`(task_id, message_id, field)`).

### Stage 1 — relevance (`process_task_updates(user_id, thread_ids)`)

For each thread (bodies read from Postgres via
`inbox_repo.load_thread_text`), one LLM call against **all active track-kind
tasks** using `prompts/relevance.py` — structurally `classify_thread.py` with
two changes: multi-label output (`{"relevant_tasks": ["<name>", ...]}` instead
of a single `bucket_name`) and per-task criteria blocks identical to today's
`<bucket name=...>` blocks. The single-pick classify call for classify-kind
tasks (inbox pills) stays as-is inside `gmail_sync._classify_batch` — two
mechanisms until Phase 4 folds classify-kind into the same call.

Results upsert `task_thread_links` with `origin='llm'` — **skipping any row
whose `origin='user'`** (pins and blocks are inviolable; this is the cheapest,
strongest correction guarantee in the system).

### Stage 2 — extraction (per newly-relevant or updated (task, thread) pair)

One LLM call via `prompts/extract_transition.py`. Prompt contains:

- the task `goal` + `state_schema` (rendered with explicit allowed values),
- the **entity registry**: current `task_state_entities` rows as
  `entity_key → {field: value}` (so the model resolves "Acme Corp" to the
  existing `acme` entity instead of minting a duplicate),
- the thread text, with per-message `message_id` markers so evidence is
  attributable,
- output contract: a JSON array of
  `{"entity": str, "is_new_entity": bool, "field": str, "new_value": str,
    "evidence_quote": str, "message_id": str, "confidence": 0-100}` — or `[]`
  ("relevant but no state change", e.g. a scheduling logistics email).

### The validator (`tasks_engine/transitions.py::validate_and_stage`) — pure code

1. **Schema check**: `field` exists in `state_schema`; `new_value` parses for
   the field kind (member of `stages` for pipelines, ISO date for `date`, etc.).
   Fail → drop with a log line (not even pending_review; malformed output is
   noise, not signal).
2. **Entity resolution**: normalize (`casefold`, strip punctuation) and match
   against `entity_key`s; pg_trgm similarity ≥ 0.6 counts as a match;
   `is_new_entity` + no match → create entity. A near-match conflict
   (similarity 0.4–0.6) → `pending_review`.
3. **Idempotency**: insert into `task_events` guarded by the partial unique
   index `(task_id, message_id, field)`; conflict → skip (re-sync replay).
4. **No-op filter**: `new_value == current` → skip.
5. **Pipeline regression flag**: moving backward in `stages` order is allowed
   (real life regresses) but forces `pending_review` regardless of confidence.
6. **Confidence gate**: `confidence >= 75` → `status='applied'` +
   `task_repo.apply_event` (write `old_value`, update entity `state`,
   `updated_at`); else `status='pending_review'` (event stored, state untouched).

Commit, then `_publish(uid, "task_updated", {"task_id": ..., "entity_ids": [...],
"event_ids": [...], "pending_count": n})` — same publish-after-commit contract
the sync path obeys (the SSE consumer re-reads via the API, so rows must exist).

### Stage 3 — backfill (`backfill_task(task_id)`)

On task creation: relevance over the user's existing `inbox_threads`
(newest-first, cap 500), then extraction over matches **in ascending
`gmail_internal_date` order** so pipelines replay history correctly
(applied → onsite → offer, not shuffled). This is `reclassify_user_inbox`'s
role generalized; in Phase 4 `reclassify_user_inbox` is deleted and
`POST /api/buckets`'s shim enqueues `backfill_task` instead. Publishes
progress events every 50 threads (`task_backfill_progress`) so the wizard can
show a live fill-up.

---

## 3. Open question (c): manual correction UX + data flow

**Position: the event log is the UI. Every piece of derived state is one click
away from the email evidence that produced it and one click away from being
overridden. User corrections are stored as first-class rows the LLM cannot
overwrite, and they feed the few-shot criteria loop.**

Surfaces (see `agent-1-sync-api-frontend.md` for routes/components):

| Correction | UI | Data flow |
|---|---|---|
| Wrong relevance (false positive) | "Detach" on a thread row in the task's thread list | `DELETE /api/tasks/{id}/threads/{thread_id}` → upsert link `origin='user', state='detached'`; all `applied` events sourced from that thread → `status='reverted'`; `refold_entity` per touched entity; optionally append the thread as a `<nearmiss>` example to `tasks.criteria` (checkbox, default on) |
| Missed relevance (false negative) | "Add to task" action in inbox/search views (task picker) | `POST /api/tasks/{id}/threads {thread_id}` → link `origin='user', state='attached'` → synchronous-enqueue `extract_for_thread(task_id, thread_id)`; optional `<positive>` example append |
| Wrong transition | "Reject"/"Revert" on an event card (feed or entity drawer) | `POST /api/tasks/{id}/events/{event_id}/reject` (pending) or `/revert` (applied) → status flip + `refold_entity` |
| Pending review | Badge on task card; review tray listing `pending_review` events with evidence quotes, Approve/Reject buttons | `/approve` → `apply_event`; `/reject` → status='rejected' |
| State just wrong | Inline edit on the entity card (e.g. drag company chip between pipeline columns) | `POST /api/tasks/{id}/entities/{entity_id}/state {field, value}` → append user-origin `applied` event (`origin='user'`, no message provenance) + apply. Fold rule: a user-origin event pins the field — later LLM events on the same field for the same entity require `pending_review` unless they cite a message newer than the user edit |
| Duplicate entity | "Merge into…" on entity card | `POST /api/tasks/{id}/entities/{entity_id}/merge {into_entity_id}` → repoint events, refold target, delete source |

All correction endpoints are synchronous FastAPI writes (no Celery — they're
single-row transactions), each followed by a `task_updated` publish so other
tabs converge via the existing SSE machinery.

The criteria-append mechanic is literally `bucket_repo.formulate_criteria`'s
example grammar: corrections accumulate as `<positive>`/`<nearmiss>` blocks on
`tasks.criteria`, so the next relevance call is better with zero new
infrastructure. Cap criteria at ~30 examples FIFO to bound prompt size.

---

## 4. Open question (d): actions and the consent model

**Position: actions are a per-task dial (`tasks.action_mode`), a closed action
vocabulary, and explicit consent at three independent layers — OAuth scope,
per-action-type grant, per-action approval. Nothing sends email without a
human click, in any mode, in this plan's horizon.**

### Vocabulary (Phase 5, deliberately tiny)

| `action_type` | Effect | Allowed in `auto`? |
|---|---|---|
| `archive_thread` | Gmail `threads.modify removeLabelIds=["INBOX"]` | yes (reversible) |
| `label_thread` | apply/create a Gmail label (e.g. "job-hunt/onsite") | yes (reversible) |
| `draft_reply` | create a Gmail **draft** (never send) | no — always propose |

### Consent layers

1. **OAuth scope.** Current scopes are read-only (`gmail.readonly`). Flipping
   any task off `action_mode='off'` requires `gmail.modify` (+
   `gmail.compose` for drafts): the client hits
   `GET /auth/login?upgrade=actions`, `auth/google_oauth.build_authorize_url`
   adds the scopes with `include_granted_scopes=true`; the callback stores the
   granted set in `users.gmail_granted_scopes`. Users who never enable actions
   are never asked.
2. **Per-task, per-action-type grant** (`task_action_grants`): enabling `auto`
   shows exactly which action types this task may auto-execute; each is an
   explicit toggle that writes a grant row. `propose` mode needs no grants.
3. **Per-action approval** (`task_actions.status`): in `propose` mode every
   action is a `proposed` row surfaced in the review tray;
   `POST /api/tasks/{id}/actions/{action_id}/approve` enqueues Celery
   `execute_task_action(action_id)` which performs the Gmail call, sets
   `executed` + `executed_at`, publishes `task_updated`. In `auto` mode,
   grant-covered reversible actions execute immediately but still write the
   row (audit) and surface an "executed — undo" toast; undo re-adds the INBOX
   label / removes the applied label.

### Where actions originate

`transitions.validate_and_stage` finishes by calling
`tasks_engine/actions.maybe_propose(task, event)`: rule-driven in v1 (no LLM
free agency) — e.g. schema authors can attach `on_enter` hooks to pipeline
stages (`{"stage": "rejected", "action": "archive_thread"}`) proposed by the
LLM at schema-draft time and shown/edited in the wizard like everything else.
LLM-composed `draft_reply` bodies use `prompts/propose_action.py` and are
always `propose`. Every action row carries `source_event_id` so the audit
chain is: email → event (evidence quote) → action.

---

## 5. Buckets become the degenerate task — unification mechanics (Phase 4)

- Migration `0009` (see `agent-1-data-model.md`): ID-preserving copy of
  `buckets` → `tasks(kind='classify')`; `inbox_threads.bucket_id` FK retargets
  to `tasks.id` with no row updates; `buckets` dropped.
- `gmail_sync._classify_batch` loads classify-kind tasks via
  `task_repo.list_classify_tasks` (replacing `bucket_repo.list_active`);
  `llm/prompts/classify_thread.py` is untouched (it already takes
  name+criteria pairs).
- Relevance (stage 1) and classification merge into **one LLM call per
  thread**: the prompt asks for `{"primary": <classify-task name|null>,
  "relevant_tasks": [<track-task names>]}` — halving LLM volume vs Phase 3.
- `reclassify_user_inbox`, `draft_preview_bucket`, `preview_cache` are deleted;
  `api/buckets.py` becomes a 30-line shim over `api/tasks.py` for one release,
  then removed along with the client's bucket modals (the task wizard with
  `kind='classify'` covers "just give me a label" creation — the schema step
  is simply skipped).
- The inbox pill, `FilterByBucketDropdown`, and `useInbox`'s
  `filterSelection` keep working throughout: they consume `bucket_id`, whose
  values never changed.
