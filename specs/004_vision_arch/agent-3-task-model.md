<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 3 — task-engine first -->

# Agent 3 — The Task Model

The precise definition of a task, its state schema language, the LLM pipeline that maps
emails to state transitions, the correction loop, and the action/consent model. The
worked find-a-job example at the end exercises every part.

## 1. Definition

A **task** is a tuple:

```
Task = (goal, relevance_criteria, state_schema?, actions_policy, status)
```

- **goal** — the user's natural-language statement ("I'm job hunting; track my
  applications"). Stored verbatim; it is the seed for everything the LLM proposes and
  the context injected into every extraction call.
- **relevance_criteria** — compiled text in the exact format buckets use today: a
  description paragraph + `Example cases:` + `<positive>`/`<nearmiss>` blocks
  (`bucket_repo.formulate_criteria` is reused unchanged). This is the input to the
  triage classifier.
- **state_schema** — an Entity-Pipeline Schema (§2), or `NULL`. `NULL` means the task is
  classify-only: **a bucket is precisely a task with `state_schema = NULL`**.
- **actions_policy** — capability grants (§5). Default `{}` = no capabilities beyond
  internal state writes.
- **status** — `active | paused | archived`. Paused tasks receive no triage matches and
  take no actions; their state is frozen but viewable.

Derived, not stored on the task row:

- **relevance set** — the threads linked to the task (`task_threads` rows), each link
  carrying its origin (`llm` or `user`) and, for user links, stickiness.
- **state** — the current entities (`task_entities`) plus the append-only event log
  (`task_events`) that produced them.

### Kinds

`tasks.kind ∈ {'bucket', 'tracker'}`. The split is presentational + policy, not
structural — both kinds run the same triage path. `bucket` additionally sets
`exclusive_group='inbox'`: within an exclusive group, triage assigns **at most one**
task per thread (preserving today's single-bucket invariant for the inbox view).
Trackers are non-exclusive: one recruiting email can feed both *find a job* and
*negotiate-comp-research*.

## 2. State representation: the Entity-Pipeline Schema (EPS)

### 2.1 The decision

Three candidate answers to VISION.md's question (a):

- **Free-form JSON the LLM maintains** — rejected. Unvalidatable (any blob is "valid"),
  undiffable (corrections can't target a field the schema doesn't name), and
  unrenderable (the HUD can't generate a pipeline board from an unknown shape). It also
  makes the correction loop meaningless: you can't mechanically enforce "the LLM may not
  move this entity backward past a user correction" on a blob.
- **Fully user-authored schemas** — rejected as the *primary* path. Asking a user to
  write a state machine before tracking their job hunt is a wall. (Power-user editing of
  the proposed schema is kept — see wizard, §2.3.)
- **LLM-proposed, user-confirmed, typed** — adopted. The bucket wizard already proved
  this exact interaction in this codebase: `draft_preview_bucket` proposes, the user
  confirms/excludes, `formulate_criteria` compiles. The task wizard does the same one
  level up: the LLM proposes an EPS from the goal; the user edits stage names, adds or
  removes attributes, and confirms. **Templates are canned proposals** in the same
  language ("Job hunt", "Apartment hunt", "Vendor selection" ship as starting points),
  not a separate mechanism.

### 2.2 The language

EPS is deliberately small: one entity type per task, one pipeline per entity. This
covers the entire vision family — find a job / apartment / therapist are all "a set of
counterparties, each progressing through stages" — and small means validatable,
renderable, and promptable. Multi-entity-type tasks are a future EPS `version: 2`, not a
v1 concern (YAGNI).

```jsonc
// tasks.state_schema (JSONB), validated by Pydantic models in app/task_engine/schema.py
{
  "version": 1,
  "entity": {
    "noun": "company",                       // what the HUD calls a card
    "identity_hint": "the company (or its recruiting agency) the email is from or about",
    "attributes": [                          // typed; the only fields extraction may write
      {"key": "role",          "type": "string"},
      {"key": "recruiter",     "type": "string"},
      {"key": "next_event_at", "type": "datetime"},
      {"key": "comp_notes",    "type": "string"}
    ]
  },
  "pipeline": {
    "stages":   ["applied", "recruiter_screen", "interview_1", "interview_2", "onsite", "offer"],
    "terminal": ["offer_accepted", "rejected", "withdrawn"],
    "allow_skip": true,                      // applied → onsite is legal (companies skip rounds)
    "allow_regress": false                   // LLM may not move an entity backward; users may
  }
}
```

Attribute types: `string | number | datetime | boolean | enum` (enum carries its
values). That's the whole type system.

**What the typing buys, concretely:**

- *Validation* — `schema.py` Pydantic models reject any extraction output naming an
  unknown stage or attribute before it touches the DB (§3.3).
- *Rendering* — `PipelineBoard` on the task page is generated: columns = `stages +
  terminal`, cards = entities, card fields = `attributes`. No per-task UI code.
- *Correction granularity* — "move Stripe back to interview_2" is a typed, legal,
  recordable operation, and the regress-protection rule (§4) is mechanically enforceable.

### 2.3 Schema lifecycle

Proposed by `llm/prompts/propose_task.py` (goal in, `{name, relevance_description,
state_schema}` out as JSON), edited in the wizard, frozen at creation. Post-creation
edits in v1 are limited to **additive** changes (new stage, new attribute, new terminal)
— additive edits cannot invalidate existing entity rows. Destructive schema edits =
archive + recreate (the backfill machinery, §3.5, makes recreation cheap). This dodges
schema-migration-of-user-data complexity until real demand exists.

## 3. The LLM pipeline: email → state transition

Two stages, both running inside the existing sync transaction in
`server/app/workers/gmail_sync.py` (which already holds `sync_lock:{uid}`, giving
per-user serialization for free).

### 3.1 Stage 1 — Triage (replaces classify)

Today: `_classify_batch` → `classify()` → one `classify_thread` prompt per thread,
returning a single bucket name. **Change:** the prompt becomes
`llm/prompts/triage_thread.py` and returns, in one call per thread:

```json
{"bucket_name": "Important" | null,
 "relevant_tasks": [{"task_name": "find a job", "confidence": 0.92}]}
```

The prompt receives all active tasks: bucket-kind tasks rendered exactly as
`classify_thread.build_user_message` renders buckets today (criteria blocks + the
stability hint for the current assignment), plus tracker-kind tasks as
`<task name=...>{relevance_criteria}</task>` blocks. Parsing keeps
`classify_thread.parse_response`'s discipline: names resolve against the shown set,
unknown/ambiguous → dropped. One Haiku-class call per thread — same cost as today's
classify; the bucket decision rides along free.

Output writes: `inbox_threads.bucket_id` (unchanged semantics) + `task_threads` upserts
for each relevant tracker — **except** where a sticky user link exists (§4): a user
detach is never re-attached by triage, a user attach is never removed.

### 3.2 Stage 2 — Extraction (new)

For each (thread, relevant tracker with a schema), `app/task_engine/engine.py` builds an
extraction call (`llm/prompts/extract_transition.py`). Inputs:

- the task `goal` + `state_schema` (stages, attributes, identity_hint),
- the **current entity list** for the task (`key`, `display_name`, `stage`, attributes) —
  so the model matches before it creates,
- the thread text via the existing `gmail/parser.thread_to_string` (full bodies — the
  Phase-0 `body_text` column makes this a DB read, not a Gmail fetch),
- the task's most recent **user-correction exemplars** (§4) as few-shot feedback.

Output (structured JSON, enforced via JSON-schema response_format — see
implementation file for the `client.call_json` addition):

```json
{
  "entity": {"match": "stripe" | null, "create": {"key": "stripe", "display_name": "Stripe"} | null},
  "transition": {"to_stage": "onsite"} | null,
  "attributes": {"next_event_at": "2026-06-19T17:00:00Z", "recruiter": "Dana K."},
  "evidence": "we'd like to invite you onsite on June 19th",
  "confidence": 0.9,
  "no_op": false
}
```

`no_op: true` is a legal and common answer ("relevant thread, no state change" — e.g. a
scheduling-logistics reply). Model: a Sonnet-class model via the existing OpenRouter
config (new `LLM_EXTRACT_MODEL` setting) — extraction mutates user-visible state and
deserves a stronger model than triage; volume is low (only relevant threads, typically
0–1 tasks each), so cost stays trivial (§3.6).

Messages within a thread are processed in `gmail_internal_date` order, and the engine
processes one extraction at a time per (task, entity-key) — out-of-order transitions
are prevented structurally, not by prompting.

### 3.3 Validation (the answer to "how is that validated?")

Validation is **mechanical and server-side** — the LLM proposes, code disposes. In
`app/task_engine/transitions.py`, every extraction output must pass, in order:

1. **Shape** — Pydantic parse against the EPS: `to_stage ∈ stages ∪ terminal`; every
   attribute key declared in the schema; values coerce to their declared types
   (datetime parse, enum membership).
2. **Entity resolution** — `match` must name an existing non-merged entity key;
   `create` requires no fuzzy-duplicate among existing keys (normalized comparison);
   exactly one of match/create unless `no_op`.
3. **Transition legality** — stage order respects `allow_skip` / `allow_regress`; an
   entity in a terminal stage can only be moved by a user.
4. **Evidence requirement** — `evidence` must appear **verbatim** in the thread text
   (substring check after whitespace normalization). This is the cheapest effective
   hallucination guard, and it's already proven in this codebase: `score_thread`'s
   `snippet` field is the same idea. No quote, no write.
5. **Correction fences** (§4) — the proposal may not contradict a user correction
   unless its evidence comes from a message *newer* than the correction.

Failures are not retried blindly and never applied: they're recorded as
`task_events(event_type='rejected_invalid', actor='system')` with the raw proposal in
`payload`, and surface in the task's review feed so systematic prompt failures are
visible. Passing proposals are applied **optimistically** — written to `task_entities`,
logged to `task_events(actor='llm')` with confidence and evidence — and the review feed
makes every application one-click reversible. (Apply-then-review beats
queue-for-approval for state writes: a tracker that waits for human approval on every
"Stripe moved to onsite" is a to-do list, not a HUD. Approval gates are reserved for
*actions*, §5.)

### 3.4 State storage shape

Materialized current state + append-only audit (not full event-sourcing — no replay
machinery, YAGNI):

- `task_entities` — one row per entity: `key`, `display_name`, `stage`, `attributes`
  JSONB, merge pointer. This is what the HUD reads.
- `task_events` — append-only: every entity creation, stage change, attribute update,
  attach/detach, correction, rejection, merge. Each row carries `actor`
  (`llm|user|system`), `from_stage/to_stage`, `evidence_quote`, soft `message_id` +
  denormalized `gmail_message_id` (audit survives inbox-row churn), `confidence`. This
  is the correction loop's substrate and the HUD's history drawer.

Every applied event bumps `tasks.version` (the SSE gap-detection counter).

### 3.5 Backfill

Creating a task mid-stream must not start from zero: `POST /api/tasks/{id}/backfill`
enqueues a Celery task that runs triage over the user's stored threads (DB-only, thanks
to `body_text`), then extraction over matches **in chronological order** — so the
pipeline reconstructs "applied in April, screened in May, onsite pending" from history.
Progress streams over SSE (`task_backfill_progress`), terminal event flips the wizard to
the populated board. Same `sync_lock` discipline as `reclassify_user_inbox`, including
its retry-on-contention pattern.

### 3.6 Cost & concurrency

Per new inbox thread: 1 triage call (Haiku-class, ≈ today's classify cost — strictly
replaces it) + K extraction calls where K = matching trackers (almost always 0; ≤1 for a
job-hunter's recruiting mail). Backfill of a 500-thread history for one new task: 500
triage + ~30 extraction calls — single-digit cents at Haiku/Sonnet pricing, bounded by
the existing shared semaphore (`LLM_CONCURRENCY=16` in `app/llm/client.py`), so a
backfill cannot starve live sync classification. No new concurrency machinery.

## 4. The correction loop (the answer to open question c)

Design rule: **every LLM write is visible, evidenced, and cheaper to undo than it was to
make.** Corrections are not exception handling; they are the training signal that makes
a task converge.

### 4.1 Correction operations

| Operation | API | Data effect | Feedback effect |
|---|---|---|---|
| Attach thread | `POST /api/tasks/{id}/threads {thread_id, op:'attach'}` | `task_threads` upsert with `link_origin='user'` → immediately runs extraction on it | Thread becomes a `<positive>` exemplar candidate for the task's criteria |
| Detach thread | same, `op:'detach'` | link marked `is_detached=true`, **kept** (it's the negative signal); any events sourced solely from this thread are reverted | Thread becomes a `<nearmiss>` exemplar; triage will never re-attach (sticky) |
| Override stage / attributes | `POST /api/tasks/{id}/entities/{eid}/correct` | entity updated; `task_events(actor='user', event_type='correction')` appended | Sets a **correction fence** (§4.2); the correction (with the user's optional note) is injected into future extraction prompts as feedback |
| Merge entities | `POST /api/tasks/{id}/entities/{eid}/merge {into}` | loser gets `is_merged_into`, events preserved under both | identity_hint feedback exemplar ("Stripe" ≡ "stripe.com recruiting") |
| Undo an LLM event | `POST /api/tasks/{id}/events/{event_id}/revert` | compensating `correction` event restoring prior state | Same fence + feedback as an override |

### 4.2 Correction fences

A user correction on an entity creates a fence at its timestamp. Extraction proposals
touching that entity are rejected by validation step 5 unless their evidence message is
**newer than the fence**. Concretely: user drags Stripe back from `onsite` to
`interview_2` → the LLM cannot re-promote Stripe by re-reading the old onsite email; only
a *new* email can move it. This is what makes optimistic application safe: the human
always wins, durably, without locks the user has to think about.

### 4.3 Feedback into prompts

The extraction prompt's feedback section is assembled per call from the task's most
recent N (≈5) user events: detaches rendered as near-miss exemplars, stage corrections
rendered as "the user corrected X→Y for {entity}: {note}". This reuses the philosophy —
and for relevance criteria, literally the function (`formulate_criteria`) — that bucket
creation already uses for `<positive>/<nearmiss>` compilation. No fine-tuning, no
embedding store; prompt-injected feedback is the v1 learning mechanism.

### 4.4 Correction UX surfaces (detail in implementation file)

- **Task page = board + review feed.** `PipelineBoard` (drag = stage correction),
  `EntityDrawer` (attribute edits + full event history with evidence quotes linking to
  threads), `ReviewFeed` (chronological LLM events, each with evidence and a revert
  button; low-confidence events visually flagged).
- **Inbox rows get task chips.** `/inbox` thread rows show attached tasks; a chip menu
  does attach/detach without leaving the inbox — the spot-check loop VISION.md says the
  inbox exists for.
- **Search-to-attach.** The task page's threads panel embeds `/api/search` (Phase-0
  FTS) so "find that recruiter email from March and attach it" is one flow.

## 5. Actions and consent (the answer to open question d)

Acting is a capability of tasks (per VISION.md — no separate automations concept), and
capabilities form a ladder. `tasks.actions_policy` (JSONB) holds explicit per-task
grants; everything is **default-deny**.

| Level | Capability | Consent model | Mechanics |
|---|---|---|---|
| **L0** | Internal: write task state, flag for review | None (this is the product working) | §3; review feed is the oversight |
| **L1** | Gmail mutations: apply label, archive, mark-read | Per-task, per-capability toggle at creation/edit time; first L1 grant triggers **OAuth re-consent** (scopes today are `gmail.readonly` — `auth/google_oauth.py` must request `gmail.modify`, an explicit user ceremony, which *is* the consent UX) | Executed by the worker post-sync; every execution is a `task_actions` row; an `Undo` (unarchive/unlabel) is kept one click away in the activity feed |
| **L2** | Outbound: reply/compose | **Propose-only, always.** The task writes a Gmail *draft* (`users.drafts.create`) and an approval card in the HUD; the user reviews in the HUD or in Gmail itself and sends. The send button is never the machine's. | `task_actions` lifecycle `proposed → approved → executed` (`executed` = draft handed off / sent by user); `rejected` proposals become feedback exemplars |

Cross-cutting rules:

- **Ledger:** every action at every level is a `task_actions` row — kind, params,
  status, evidence (which event triggered it), timestamps. The HUD's activity feed reads
  this table; "what did this task do while I was away" is a query, not a vibe.
- **Pause is absolute:** `tasks.status='paused'` halts triage matching, extraction, and
  all actions for the task instantly (checked at engine entry, not queue-drain).
- **Standing rules** ("always archive these") are just L1 grants scoped by the task's
  relevance set — the degenerate-bucket lineage makes "auto-archive the Marketing
  bucket" the first shippable action with zero new concepts.
- **No autonomous send. Period, in this plan's horizon.** Revisiting requires its own
  spec with rate caps and recipient allowlists; nothing below depends on it.

## 6. The toy example, end to end: *find a job*

**1 — Create.** Maya opens the HUD, hits *New task*, types: *"I'm job hunting. Track
every company I'm in process with and where I stand."* → `POST /api/tasks/draft`. The
worker (`propose_task` prompt) returns a proposal: name *"Job hunt"*, relevance
description ("emails from recruiters, hiring managers, or application systems about
roles Maya applied to…"), and the EPS from §2.2. The wizard renders the pipeline as an
editable stage list — Maya renames `interview_2` to `panel`, deletes `comp_notes`,
keeps the rest. Next step: relevance preview — the same machinery as today's
`draft_preview_bucket` scores her stored threads against the proposed criteria and
shows top positives ("Your application to Stripe", "Coffee chat? — Anthropic
recruiting") and near-misses ("LinkedIn: 8 jobs for you" — she confirms it as a
near-miss). Confirm → `POST /api/tasks` compiles criteria via `formulate_criteria`,
inserts the `tasks` row (`kind='tracker'`, the edited EPS, `actions_policy={}`,
`version=1`), and enqueues backfill.

**2 — Backfill.** The backfill task triages her ~400 stored threads from `body_text`
(no Gmail calls), matches 23, runs extraction chronologically. The board populates
live over SSE: **Stripe** (applied → recruiter_screen → interview_1, `role:
"Product Engineer"`), **Anthropic** (recruiter_screen), **Figma** (rejected,
terminal), … each transition carrying its evidence quote. Wizard closes into the task
page.

**3 — An email arrives.** Tuesday 9:14am, Stripe's recruiter: *"…we'd like to invite
you onsite on June 19th…"*. Within ≤30s the beat poll fires `poll_new_messages` →
`partial_sync_inbox` upserts the thread (now with full `body_text`) → **triage**:
`{"bucket_name": "Important", "relevant_tasks": [{"task_name": "Job hunt",
"confidence": 0.95}]}` → `task_threads` upsert → **extraction**: matches entity
`stripe`, proposes `to_stage: "onsite"`, `next_event_at: 2026-06-19`, evidence *"invite
you onsite on June 19th"*, confidence 0.9. Validation: stage legal
(interview_1→onsite, `allow_skip`), attribute typed, evidence found verbatim, no fence
→ applied. `task_entities` updated, `task_events` appended, `tasks.version` 41→42,
worker publishes `task_updated {task_id, version: 42}` on `user:{uid}` alongside the
usual `threads_updated`.

**4 — Maya views the HUD.** Her open tab's EventSource delivers both frames;
`useTaskDetail` sees version 42 vs local 41 → refetches `GET /api/tasks/{id}` → the
Stripe card slides to the **onsite** column with a "9:14am · LLM · ⏎ evidence" badge.
The HUD home card for *Job hunt* now reads `applied 4 · screen 6 · interviews 3 ·
onsite 1 · offers 0`, freshness ticker "synced 12s ago". Had she been offline all week,
the hourly task-owner poll would have kept this current anyway.

**5 — Maya corrects an error.** She notices **"Stripe Atlas"** appeared as a second
company — the LLM mis-split the recruiting thread from a Stripe product
announcement. She opens the Stripe Atlas card's drawer, sees the single sourcing
thread, and clicks **detach** → the entity (sourced solely from that thread) is
reverted, the link survives as `is_detached=true`, and the thread is rendered into the
task's prompt feedback as a near-miss. Separately, the panel interview she actually
bombed: she drags **Notion** from `onsite` back to `panel`; a `correction` event
(actor=user) is appended with her note "onsite was actually the panel round", a fence
is set — no stale email can re-promote Notion — and the next extraction call for this
task carries her correction as feedback. Total cost of both fixes: four clicks, fully
audited, and the task got smarter.

**6 — (Phase 4) Action.** Maya later grants the task L1 `label` ("label matched
threads *Job hunt*" — triggering the one-time `gmail.modify` re-consent) and enables an
L2 rule: when a recruiter asks for availability, draft a reply from her calendar-stub
template. Wednesday: Anthropic's recruiter asks for times → the task creates a Gmail
draft + an approval card; Maya edits one line in the HUD card and hits send herself.
The `task_actions` ledger shows both: `executed: label` (auto), `executed: draft_reply`
(approved by user).
