<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 2 — data-layer first -->

# Task Engine — data model, pipeline, and positions on VISION.md's open questions

> Built strictly on the guarantees from phases 0–3: full bodies in Postgres
> (G4 — extraction never refetches Gmail), stable thread identity (G3 — links
> and evidence never orphan), push-fresh data (G1), replayable events (G6).

## 1. Data model (migration `0008_tasks.py`, Phase 4)

```python
op.create_table("tasks",
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), index=True),  # NULL = default (bucket-kind only)
    sa.Column("kind", sa.String(16), nullable=False, server_default="tracker"),  # 'tracker' | 'bucket'
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("goal", sa.Text(), nullable=False, server_default=""),             # the user's natural-language goal
    sa.Column("criteria", sa.Text(), nullable=False, server_default=""),         # relevance criteria, same format as Bucket.criteria
    sa.Column("schema_json", sa.dialects.postgresql.JSONB()),                    # NULL for kind='bucket' (the degenerate task)
    sa.Column("action_policy", sa.dialects.postgresql.JSONB(), server_default='{}'),  # Phase 6, {action_kind: off|propose|auto}
    sa.Column("status", sa.String(16), nullable=False, server_default="active"), # active | paused | archived
    sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

op.create_table("task_entities",   # e.g. one company in "find a job"
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), index=True, nullable=False),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), index=True, nullable=False),
    sa.Column("key", sa.String(255), nullable=False),       # canonical name ("Stripe"); uq (task_id, key)
    sa.Column("stage", sa.String(64)),                      # current pipeline stage, projection of applied events
    sa.Column("attrs", sa.dialects.postgresql.JSONB(), server_default='{}'),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("task_id", "key", name="uq_task_entities_task_key"),
)

op.create_table("task_links",      # relevance: thread ↔ task (many-to-many)
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), index=True, nullable=False),
    sa.Column("thread_id", sa.String(36), sa.ForeignKey("inbox_threads.id"), index=True, nullable=False),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), index=True, nullable=False),
    sa.Column("entity_id", sa.String(36), sa.ForeignKey("task_entities.id"), index=True),  # nullable: linked but unmapped
    sa.Column("source", sa.String(8), nullable=False),      # 'llm' | 'user'
    sa.Column("state", sa.String(16), nullable=False, server_default="attached"),  # attached | detached
    sa.Column("confidence", sa.Integer()),                  # 0-10 at link time (llm source)
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("task_id", "thread_id", name="uq_task_links_task_thread"),
)

op.create_table("task_events",     # append-only ledger; entity state = fold of applied events
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), index=True, nullable=False),
    sa.Column("entity_id", sa.String(36), sa.ForeignKey("task_entities.id"), index=True),
    sa.Column("message_id", sa.String(36), sa.ForeignKey("inbox_messages.id")),  # evidence anchor (NULL for manual)
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), index=True, nullable=False),
    sa.Column("type", sa.String(16), nullable=False),       # transition | attr_update | note | correction | action
    sa.Column("from_stage", sa.String(64)), sa.Column("to_stage", sa.String(64)),
    sa.Column("payload", sa.dialects.postgresql.JSONB(), server_default='{}'),   # evidence quote, rationale, action params/results
    sa.Column("proposed_by", sa.String(8), nullable=False), # 'llm' | 'user'
    sa.Column("confidence", sa.Integer()),
    sa.Column("status", sa.String(16), nullable=False, server_default="applied"),# proposed | applied | rejected | reverted
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
```

Why a ledger instead of mutable state: corrections (q-c) become appends, the
HUD gets an evidence timeline for free, and a bad extraction batch is
revertible without data archaeology. `task_entities.stage` is a denormalized
projection maintained on every applied/reverted event (revert recomputes from
the remaining applied events for that entity).

SQLAlchemy models `Task`, `TaskEntity`, `TaskLink`, `TaskEvent` go in
`server/app/db/models.py`; repo layer `server/app/inbox/task_repo.py`
(read/write, never commits — same contract as `inbox_repo`/`bucket_repo`).

## 2. VISION open question (a): defining the state representation

**Committed: LLM-proposed from a natural-language goal, user-confirmed,
constrained to a fixed meta-schema.** Not freeform user-authored (schema
design is expert work; users have goals, not schemas). Not templates-only
(too rigid for "any sustained effort"). Templates *emerge*: a confirmed
schema is saveable and the picker offers past schemas — but that's a UI
affordance, not the mechanism.

The meta-schema (`tasks.schema_json`) is deliberately narrow in v1:

```json
{
  "entity_noun": "company",
  "stages": ["applied", "phone screen", "onsite", "offer", "rejected"],
  "terminal_stages": ["offer", "rejected"],
  "attrs": [{"key": "role", "type": "string"}, {"key": "next_event_at", "type": "datetime"}]
}
```

One entity type, one enum stage pipeline, optional typed attrs. This bounds
the extraction prompt, the validation logic, and the board UI to one shape.
Richer representations (multiple entity types, numeric rollups) are
explicitly post-v1.

Creation flow reuses the draft-preview machinery
(`server/app/inbox/preview_cache.py` + a new `draft_task` Celery task
mirroring `draft_preview_bucket` in `server/app/workers/tasks.py`):

1. User types name + goal ("find a job").
2. Worker LLM call #1 (`llm/prompts/propose_task_schema.py`, new): goal →
   proposed `schema_json` + relevance criteria text + 5–10 FTS keyword probes.
3. Worker uses the keyword probes against `search_repo.search_threads` to
   prefilter candidates (the FTS payoff: no need to LLM-score the whole
   inbox), then scores top candidates with the existing `score_thread` prompt.
4. Cache-before-publish (same `preview:{draft_id}` pattern), SSE
   `bucket_draft_preview`-style event.
5. Wizard shows: editable stage list, editable criteria, candidate threads
   with confirm/reject — confirmations become `<positive>`/`<nearmiss>`
   blocks via `bucket_repo.formulate_criteria` (lifted to `task_repo`).
6. `POST /api/tasks` persists; backfill task links + initial extraction run.

## 3. VISION open question (b): emails → state transitions, and validation

**Committed: a two-stage LLM pipeline (relevance, then extraction) with
mechanical validation and a confidence gate.**

Stage 1 — relevance (extends the existing classifier): `enrich_threads`
(Phase 3's enrichment task) runs one LLM call per thread against the user's
active task set — prompt evolved from
`server/app/llm/prompts/classify_thread.py` to return **all** matching tasks,
not a single pick:

```json
{"matches": [{"task_name": "find a job", "confidence": 9}, ...]}
```

- Bucket-kind tasks: highest-confidence bucket match becomes the displayed
  bucket (preserving today's single-bucket semantics during transition).
- Tracker-kind matches with confidence ≥7 → upsert `task_links(source='llm')`.
- Threads with `task_links.state='detached'` + `source='user'` are **hard
  negatives**: injected into the prompt as `<nearmiss>` examples and
  short-circuited in code (a user detach can never be re-attached by the LLM).

Stage 2 — extraction, per (newly linked or updated thread × tracker task):
new prompt `server/app/llm/prompts/extract_task_event.py`. Inputs: thread
text (`thread_to_string`, from Postgres), `schema_json`, the entity roster
(keys + current stages), the entity's recent applied events. Output:

```json
{"entity_key": "Stripe", "entity_is_new": false,
 "event": {"type": "transition", "to_stage": "onsite",
           "evidence_quote": "we'd like to invite you onsite",
           "rationale": "...", "confidence": 8},
 "attrs": {"next_event_at": "2026-06-20T15:00:00Z"}}
```

(or `"event": null` for relevant-but-no-news.)

**Validation, mechanical (this is the answer to "how is it validated"):**

1. `to_stage` must be in `schema_json.stages`; unknown → reject to review.
2. `evidence_quote` must substring-match the message's `body_text` or
   subject (case/whitespace-normalized) — a fabricated quote fails closed.
3. Confidence ≥7 AND the transition is "forward or lateral" in stage order →
   `status='applied'`, entity projection updated. Backward transitions
   (onsite → applied) and confidence 4–6 → `status='proposed'` (review
   queue). Confidence <4 → discarded.
4. Idempotence: one applied `transition` per (entity, message) — re-runs
   upsert, never duplicate.
5. Entity dedup: `entity_is_new` resolved against `uq_task_entities_task_key`
   after canonicalization (trim/casefold); near-duplicate keys ("Stripe" vs
   "Stripe, Inc.") surfaced in review rather than auto-merged (v1).

Applied/proposed results commit, then `event_log.append(task_state_changed /
task_review_pending)` — publish-after-commit, same invariant as sync.

## 4. VISION open question (c): manual correction

**Committed: correction is a first-class HUD loop with three gestures, all
writing the same ledger.**

| Gesture | Surface | Data flow |
|---------|---------|-----------|
| Attach / detach thread | any thread row (inbox, search, task view) | `POST /api/tasks/{id}/links {thread_id}` / `DELETE .../links/{thread_id}` → `task_links` upsert (`source='user'`); detach also reverts that thread's applied events; attach enqueues extraction for the thread |
| Approve / reject proposed event | per-task review queue | `POST /api/tasks/{id}/events/{event_id}/resolve {approve\|reject}` → status flip + entity projection update |
| Fix a wrong transition | entity timeline | `POST .../events/{event_id}/revert` (status='reverted', projection recomputed) and/or `POST /api/tasks/{id}/events` manual event (`proposed_by='user'`, applied immediately) |

Feedback loop: every user correction appends a tagged example to
`tasks.criteria` (the `<positive>`/`<nearmiss>` format the classifier already
consumes — same mechanism that makes bucket creation work today), so the
relevance stage learns. Extraction corrections store the (message, corrected
event) pair in `payload` for future few-shot use (collected from day one,
prompt-injected when we have ≥3 per task).

How much UI is built around this: the **task board's review queue is a
primary panel, not buried settings** — the HUD's credibility depends on
corrections being one click. Budget ~30% of Phase 4 frontend scope for it.

## 5. VISION open question (d): actions and consent

**Committed v1 action kinds — exactly two:**

1. `create_draft` — write a Gmail draft reply on a linked thread (never
   sends).
2. `modify_labels` — archive / label a linked thread in Gmail.

**Consent model:** `tasks.action_policy` maps action kind → `off` (default) |
`propose` | `auto`.

- `propose`: the LLM emits a `task_events(type='action', status='proposed')`
  row with full params in `payload` (e.g. the draft text); it renders in the
  review queue; user approval executes it and flips to `applied` with the
  result (draft id, label change) recorded.
- `auto`: permitted **only** for reversible actions (`modify_labels`).
  `create_draft` caps at `propose` in v1; actual sending doesn't exist in v1
  at all. This line is the consent model: *nothing leaves the user's account
  without a click, ever, in v1.*
- Every execution is a ledger row — the audit trail is the same table the
  HUD already renders.

OAuth: current scopes are `gmail.readonly` + identity
(`server/app/auth/google_oauth.py`). Enabling any action triggers an
**incremental re-consent** flow (`/auth/login?scopes=actions` →
`gmail.modify` (+ `gmail.compose` for drafts)); `users` gains
`granted_scopes Text` (migration 0008) and action endpoints 403 with
`{"error": "scope_upgrade_required"}` until granted. Users who never enable
actions never see the scarier consent screen.

## 6. Buckets are the degenerate task — unification (Phase 5)

Explicit how/when:

- **Phase 4:** `buckets` table and `inbox_threads.bucket_id` untouched; tasks
  run alongside. (De-risks the engine behind a stable feature.)
- **Phase 5, migration `0009_bucket_unification.py`:**
  1. Insert one `tasks` row per `buckets` row (`kind='bucket'`,
     `criteria=buckets.criteria`, `schema_json=NULL`,
     `is_deleted=buckets.is_deleted`; default buckets keep `user_id=NULL`).
  2. Backfill `task_links` from `inbox_threads.bucket_id` (`source='llm'`,
     `state='attached'`).
  3. Add `inbox_threads.display_task_id` (FK tasks.id) = the single
     bucket-kind link, maintained by enrichment; drop `bucket_id` one release
     later (two-step for rollback).
  4. `/api/buckets*` routes in `server/app/api/buckets.py` become thin
     aliases over `task_repo` filtered `kind='bucket'`; client migrates to
     `/api/tasks?kind=bucket`; aliases removed next release.
  5. `bucket_repo.list_active`, `_classify_batch`'s bucket loading, and
     `reclassify_user_inbox` re-point at `tasks(kind='bucket')`;
     `reclassify_user_inbox` generalizes to `re_enrich_user_inbox` (relevance
     for all kinds + extraction for trackers).
- A bucket "upgrade" affordance falls out: edit a bucket-kind task, add a
  goal + schema → `kind='tracker'`. That UX moment *is* the vision's
  unification made tangible.

## 7. Cost & throughput notes

- Steady-state per new thread: 1 relevance call + (matched trackers ×
  1 extraction call), all under the existing
  `Semaphore(LLM_CONCURRENCY=16)` on the worker LLM loop thread
  (`server/app/llm/client.py`); models via OpenRouter config
  (`LLM_CLASSIFY_MODEL`, new `LLM_EXTRACT_MODEL`, both default
  `anthropic/claude-haiku-4-5`).
- Task-creation backfill is FTS-prefiltered (§2 step 3) — never "score the
  whole inbox," which is what makes deep history (extend) affordable.
- Extraction reads bodies from Postgres (G4); zero Gmail calls in the loop.
