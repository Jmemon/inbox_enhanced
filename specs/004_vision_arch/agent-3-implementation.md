<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 3 — task-engine first -->

# Agent 3 — Implementation Plan

Code-level mapping of the task model (`agent-3-task-model.md`) onto the existing stack:
FastAPI + Celery + Postgres + Redis + React 19/Vite on Railway. Every section names the
real files and symbols it touches. No component is replaced.

## 1. Data model (SQLAlchemy + Alembic)

New module convention: the engine lives in **`server/app/task_engine/`** — deliberately
*not* `app/tasks/`, which would collide mentally and in imports with the Celery module
`app/workers/tasks.py`.

### 1.1 Models (`server/app/db/models.py` additions)

```python
class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), index=True)  # None = default (shared), same convention as Bucket
    kind: Mapped[str] = mapped_column(String(16), nullable=False)        # 'bucket' | 'tracker'
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    relevance_criteria: Mapped[str] = mapped_column(Text, nullable=False, default="")  # formulate_criteria output
    state_schema: Mapped[dict | None] = mapped_column(JSONB)             # EPS; NULL = degenerate (bucket)
    exclusive_group: Mapped[str | None] = mapped_column(String(32))      # 'inbox' for bucket-kind
    actions_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active|paused|archived
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)        # SSE gap detection
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

class TaskThread(Base):                       # relevance links
    __tablename__ = "task_threads"
    __table_args__ = (UniqueConstraint("task_id", "thread_id", name="uq_task_threads_task_thread"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("inbox_threads.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    link_origin: Mapped[str] = mapped_column(String(8), nullable=False)  # 'llm' | 'user'  (user links are sticky)
    is_detached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)   # user-detached = durable negative
    confidence: Mapped[float | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

class TaskEntity(Base):                       # materialized current state
    __tablename__ = "task_entities"
    __table_args__ = (UniqueConstraint("task_id", "key", name="uq_task_entities_task_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)        # normalized identity ('stripe')
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_merged_into: Mapped[str | None] = mapped_column(String(36))       # soft pointer to surviving entity
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

class TaskEvent(Base):                        # append-only audit / correction history
    __tablename__ = "task_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), index=True)  # soft (events outlive merges)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(8), nullable=False)        # 'llm' | 'user' | 'system'
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
        # entity_created | stage_changed | attributes_updated | thread_attached | thread_detached
        # | correction | reverted | rejected_invalid | entity_merged
    from_stage: Mapped[str | None] = mapped_column(String(64))
    to_stage: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # attr deltas, user note, raw rejected proposal
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(String(36))           # soft ptr to inbox_messages (may churn)
    gmail_message_id: Mapped[str | None] = mapped_column(String(64))     # denormalized: audit survives inbox wipes
    confidence: Mapped[float | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

class TaskAction(Base):                       # Phase 4
    __tablename__ = "task_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)        # label | archive | draft_reply | ...
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False)      # proposed|approved|rejected|executed|failed|auto_approved
    source_event_id: Mapped[str | None] = mapped_column(String(36))      # which task_event triggered it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

Correction fences need no extra table: a fence = the latest
`task_events(actor='user', entity_id=E)` row; `transitions.py` compares the proposal's
evidence message `gmail_internal_date` against that event's `created_at`.

### 1.2 Migrations (`server/migrations/versions/`)

Following the style of `0003_buckets_v2.py` (data migration with deterministic ids and
reversible `downgrade`):

- **`0006_message_bodies_fts`** (Phase 0)
  1. `ADD COLUMN inbox_messages.body_text TEXT` (nullable — old rows backfill on next
     touch; `gmail/parser.ParsedMessage.body_text` already carries the value, `inbox_repo.upsert_message`
     gains the param).
  2. Generated column `inbox_messages.search_tsv tsvector GENERATED ALWAYS AS
     (to_tsvector('english', coalesce(body_text,'') )) STORED` + GIN index; a matching
     tsvector on `inbox_threads.subject`.
- **`0007_tasks`** (Phase 1)
  1. Create `tasks`, `task_threads`, `task_entities`, `task_events`.
  2. Backfill: `INSERT INTO tasks (id, user_id, kind, name, relevance_criteria,
     exclusive_group, ...) SELECT id, user_id, 'bucket', name, criteria, 'inbox', ...
     FROM buckets WHERE NOT is_deleted` — **reusing bucket ids** so
     `inbox_threads.bucket_id` values remain valid task ids with zero repointing.
     (Soft-deleted buckets copy over as `status='archived'`.)
  3. Materialize assignments: one `task_threads(link_origin='llm')` row per
     `inbox_threads` row with non-null `bucket_id`.
  4. `buckets` table and `inbox_threads.bucket_id` are **kept** (dual-write during
     transition, §2.2); downgrade drops the four tables.
- **`0008_task_actions`** (Phase 4) — `task_actions` table.
- **`000N_drop_buckets`** (final) — after the SPA reads tasks everywhere: drop
  `inbox_threads.bucket_id`, drop `buckets`.

## 2. Buckets unification mechanics

### 2.1 Code mapping

| Today | Becomes |
|---|---|
| `inbox/bucket_repo.list_active(db, user_id)` | `task_engine/repo.list_active(db, user_id, kind=None)` — same defaults-plus-owned query shape against `tasks` |
| `bucket_repo.create_custom / rename / soft_delete` | `repo.create / rename / archive` (archive = today's soft delete) |
| `bucket_repo.formulate_criteria` | moves verbatim to `task_engine/criteria.py` (it is the relevance compiler; zero changes) |
| `llm/prompts/classify_thread.py` | superseded by `llm/prompts/triage_thread.py`; `parse_response`'s name→id resolution discipline is carried over |
| `gmail_sync._classify_batch` | `gmail_sync._triage_batch` — loads active tasks once, returns `(bucket_pick, relevant_task_ids)` per thread; bucket pick still written to `inbox_threads.bucket_id` during transition |
| `workers/tasks.reclassify_user_inbox` | generalized to `retriage_user_inbox(user_id, task_id=None)` — same lock/retry/two-phase shape, now also used as the **backfill** runner when `task_id` is given (triage scoped to one task + chronological extraction) |
| `draft_preview_bucket` + `preview_cache` | reused as-is for the task wizard's relevance-preview step (the scoring prompt `score_thread.py` is task-agnostic already: name + description in, score/rationale/snippet out) |

### 2.2 Cutover sequence (zero-regression)

1. Migration 0007 lands; `repo` reads `tasks`, API `app/api/buckets.py` endpoints become
   shims calling `task_engine/repo` with `kind='bucket'` (response shape unchanged —
   `_serialize` keeps `{id, name, criteria, is_default}`).
2. Sync path dual-writes: `_triage_batch` writes both `inbox_threads.bucket_id` and
   `task_threads`. Client untouched.
3. Phase 3 ships `/api/tasks` to the client everywhere; bucket endpoints deleted;
   `000N_drop_buckets` runs.

## 3. LLM layer (`server/app/llm/` + `server/app/task_engine/`)

```
server/app/llm/
├── client.py                  # MODIFIED: add call_json(model, system, user, json_schema) →
│                              #   chat.completions.create(..., response_format={"type":"json_schema",...})
│                              #   same semaphore, same ""-on-error degradation; add LLM_EXTRACT_MODEL
│                              #   (config.py: llm_extract_model, default "anthropic/claude-sonnet-4-5")
├── prompts/
│   ├── triage_thread.py       # NEW: replaces classify_thread; bucket pick + multi-label task relevance
│   ├── extract_transition.py  # NEW: EPS + current entities + thread + feedback → transition JSON
│   ├── propose_task.py        # NEW: NL goal → {name, relevance_description, state_schema}
│   └── score_thread.py        # UNCHANGED: reused by task wizard preview
server/app/task_engine/
├── schema.py                  # Pydantic EPS models (EntitySpec, AttributeSpec, PipelineSpec, TaskStateSchema)
├── repo.py                    # task/entity/event/link CRUD; never commits (same contract as inbox_repo)
├── criteria.py                # formulate_criteria (moved)
├── transitions.py             # the 5-step validator (shape, entity resolution, legality, evidence, fences)
└── engine.py                  # orchestration: triage_threads(), extract_for_task(), apply(), backfill_plan()
```

Worker wiring: `gmail_sync.partial_sync_inbox` / `full_sync_inbox` /
`extend_inbox_history` call `engine.triage_threads` where they call `_classify_batch`
today, then `engine.extract_for_task` per (thread, relevant tracker) — all inside the
already-held `sync_lock`, committed in the same transaction as the thread upserts, and
the touched `task_id`s are published after `db.commit()` via the existing
`tasks._publish` (`workers/tasks.py:52`) as `task_updated {task_id, version}` frames.

## 4. Substrate changes (Phase 0 + Phase 3 details)

1. **`body_text`** — `inbox_repo.upsert_message` gains `body_text`;
   `gmail_sync._upsert_thread_with_messages` passes `m.body_text` through. Reads:
   `engine` rebuilds `ParsedThread`s from DB rows (new
   `inbox_repo.load_parsed_threads(db, user_id, thread_ids)` returning the
   `gmail/parser.ParsedThread` dataclass), which lets `_reclassify_all` and
   `_score_all` in `workers/tasks.py` drop their sequential
   `gmail.users().threads().get(format="full")` loops — the single biggest latency/quota
   win in the repo, and a hard prerequisite for backfill.
2. **Non-destructive full sync** — `gmail_sync.full_sync_inbox` stops calling
   `inbox_repo.clear_user_inbox`; it upserts the newest-200 listing (the
   `(user_id, gmail_id)` unique constraints already make this safe) and advances the
   cursor as today. Threads outside the window simply persist. `clear_user_inbox` is
   retained only for account disconnect.
3. **Search** — `GET /api/search?q=&limit=` in a new `app/api/search.py`:
   `websearch_to_tsquery` against the 0006 tsvectors, user-scoped, returns the
   `_serialize_thread` shape from `app/api/inbox.py` so the client reuses thread
   rendering.
4. **Task-owner offline polling (Phase 3)** — `workers/beat_schedule.py` gains
   `enqueue-task-owner-polls-hourly`; the new task selects
   `DISTINCT user_id FROM tasks WHERE kind='tracker' AND status='active'`, excludes
   currently-active users (they're on the 30s path), and spreads
   `poll_new_messages.apply_async(countdown=hash(uid) % 3600)`. Beat stays single
   replica per `railway.beat.toml`.
5. **Realtime** — no transport change. New event types over the existing channel:
   `task_updated {task_id, version}`, `task_backfill_progress {task_id, done, total}`,
   `task_draft_ready {draft_id}` (wizard), later `task_action_proposed`. Client-side
   version-gap refetch replaces the watchdog-`setTimeout` pattern in `Home.tsx` for
   task state (the inbox watchdogs stay until Phase 3 retires them).

## 5. API surface (`server/app/api/tasks.py`, registered in `app/main.py`)

```
POST   /api/tasks/draft                       {goal} → 202 {draft_id}
       worker: propose_task → relevance preview scoring (reuse draft_preview machinery)
       → preview_cache.store_result; SSE task_draft_ready; GET polling fallback:
GET    /api/tasks/draft/{draft_id}            202 pending | 200 {proposal, positives, near_misses}
POST   /api/tasks                             {name, goal, description, state_schema?, confirmed_positives,
                                               confirmed_negatives} → 201 task; enqueues backfill
GET    /api/tasks                             list (kind filter; HUD home reads this + per-task stage counts)
GET    /api/tasks/{id}                        snapshot: task + entities (non-merged) + version
GET    /api/tasks/{id}/events?since=&limit=   review feed / history drawer
POST   /api/tasks/{id}/threads                {thread_id, op: attach|detach}
GET    /api/tasks/{id}/threads                attached threads (serialize like /api/inbox)
POST   /api/tasks/{id}/entities/{eid}/correct {stage?, attributes?, note?}
POST   /api/tasks/{id}/entities/{eid}/merge   {into_entity_id}
POST   /api/tasks/{id}/events/{event_id}/revert
POST   /api/tasks/{id}/backfill               202; progress via SSE
PATCH  /api/tasks/{id}                        rename, pause/resume, additive schema edits, actions_policy
DELETE /api/tasks/{id}                        archive (soft; same idempotency contract as delete_bucket)
GET    /api/search?q=                         Phase-0 FTS
Phase 4:
GET    /api/tasks/{id}/actions                ledger + pending approvals
POST   /api/tasks/{id}/actions/{aid}          {op: approve|reject}
```

Auth: every route under `Depends(get_current_user)` (`app/deps.py`), ownership checks
following the `_load_owned_or_403` pattern in `app/api/buckets.py` (default tasks
immutable by users). Mutating routes (`correct`, `threads`, `revert`) bump
`tasks.version` and publish `task_updated` so other tabs converge.

## 6. Frontend: the HUD-first inversion (`client/src/`)

Routing today is a state machine in `App.tsx` (loading/anon/authed → `<Home>`).
Phase 2 adds **`react-router-dom`** (via bun) — three+ routes with deep links
(`/tasks/:id`) is past the threshold where hand-rolled routing pays.

```
client/src/
├── App.tsx                    # MODIFIED: RouterProvider; authed shell with top nav (HUD | Inbox)
├── lib/api.ts                 # MODIFIED: task wrappers (getTasks, getTask, getTaskEvents, postTaskDraft,
│                              #   createTask, attachThread, correctEntity, revertEvent, search, ...)
├── lib/sse.ts                 # UNCHANGED: singleton already broadcasts typed JSON frames
└── pages/
    ├── hud/                                  # NEW — the product surface
    │   ├── Hud.tsx                           # route '/': task card grid + activity ticker + New Task
    │   ├── TaskCard.tsx                      # name, stage-count summary, last event, freshness
    │   ├── ActivityTicker.tsx                # last ~10 processed emails/events (specs/003 'how up-to-date')
    │   ├── NewTaskWizard.tsx                 # steps: goal → schema-edit → relevance preview → creating
    │   │                                     #   (structure cloned from NewBucketModal's form|pending|review
    │   │                                     #    + its SSE-or-poll applyPreview idempotency pattern)
    │   ├── SchemaEditor.tsx                  # editable stage list + attribute rows over the proposed EPS
    │   └── useTasks.tsx                      # list + SSE 'task_updated' → per-task version map
    ├── task/                                 # NEW — route '/tasks/:id'
    │   ├── TaskDetail.tsx                    # layout: board | review feed | threads panel
    │   ├── PipelineBoard.tsx                 # columns from state_schema.pipeline; drag = correct(stage)
    │   ├── EntityDrawer.tsx                  # attributes, event history w/ evidence quotes, merge, undo
    │   ├── ReviewFeed.tsx                    # chronological LLM events; confidence flags; revert buttons
    │   ├── ThreadsPanel.tsx                  # attached threads + search-to-attach (GET /api/search)
    │   └── useTaskDetail.tsx                 # snapshot fetch; on SSE version gap → refetch (replaces
    │                                         #   the Home.tsx watchdog-setTimeout pattern for tasks)
    ├── inbox/                                # demoted to route '/inbox' (Phase 3)
    │   ├── (existing files unchanged)        # InboxList/Pagination/useInbox/useInboxSse
    │   └── TaskChips.tsx                     # NEW: per-row task chips + attach/detach menu
    └── buckets/                              # shrinks: FilterByBucketDropdown survives on /inbox reading
                                              #   tasks(kind='bucket'); NewBucketModal folds into NewTaskWizard
                                              #   (a bucket is the wizard with schema step skipped)
```

Phase staging: Phase 2 mounts `/tasks/:id` + the wizard while `/` is still the inbox
(`Home.tsx` intact). Phase 3 flips the root: `Hud.tsx` at `/`, inbox at `/inbox`,
`Home.tsx` dissolved into the shell + `/inbox` page. The flip is a route-table change,
not a rewrite — which is the point of building the task pages first.

## 7. Phases with scope

**Phase 0 — Substrate floor** (small: ~2 migrations, ~6 files)
`0006_message_bodies_fts`; `inbox_repo.upsert_message` + `load_parsed_threads`;
`gmail_sync` non-destructive full sync + pass-through `body_text`; rewrite
`_reclassify_all`/`_score_all` to read from DB; `app/api/search.py`; search box on the
inbox header. Tests: upsert-not-wipe full sync, FTS round-trip, reclassify-without-Gmail.

**Phase 1 — Task model under buckets** (medium: 1 migration, repo + prompt swap, shims)
`0007_tasks`; `task_engine/{repo,criteria}.py`; `triage_thread.py` + `_triage_batch`
dual-write; `app/api/buckets.py` → shim; `retriage_user_inbox`. Ship gate: existing
pytest suite green with bucket endpoints shimmed; UX byte-identical.

**Phase 2 — Trackers MVP** (large: the engine + wizard + task page)
`task_engine/{schema,transitions,engine}.py`; `extract_transition.py`,
`propose_task.py`; `client.call_json` + `LLM_EXTRACT_MODEL`; draft/backfill Celery
tasks + `preview_cache` reuse; `/api/tasks*` routes; react-router; `pages/task/*`,
`NewTaskWizard`, `useTasks`/`useTaskDetail`; `task_updated` versioned SSE. Ship gate:
the §6 worked example passes end to end against a real Gmail account, including both
corrections.

**Phase 3 — HUD inversion** (medium: mostly client + one beat entry)
`Hud.tsx` at `/`; inbox → `/inbox` + `TaskChips`; `ActivityTicker`; `ReviewFeed`
polish; hourly task-owner poll in `beat_schedule.py`; retire `Home.tsx` watchdogs in
favor of version-gap refetch.

**Phase 4 — Actions** (medium-large: scope ceremony is the hard part)
`0008_task_actions`; `actions_policy` editor; `gmail.modify` re-consent flow in
`auth/google_oauth.py` (incremental auth: detect granted scopes, re-prompt on first L1
grant); L1 executor in worker post-sync; L2 `drafts.create` + approval queue UI;
`task_action_proposed` SSE.

**Phase 5 — Substrate on demand** (each gated on a measured deficiency)
Gmail `users.watch` + Pub/Sub push replacing/augmenting the 30s beat poll (keep poll as
fallback; beat single-replica invariant holds); pgvector semantic search if FTS recall
disappoints during real EDA use; blob storage if full bodies strain Postgres.

`000N_drop_buckets` rides whichever release follows two clean weeks of Phase-3
operation.

## 8. Testing & ops notes

- All new repos follow the never-commit contract (`inbox_repo`/`bucket_repo` docstrings)
  so `CELERY_TASK_ALWAYS_EAGER=1` tests keep wrapping tasks in one transaction.
- `transitions.py` is pure (no IO) — the validator gets exhaustive unit tests: illegal
  stage, unknown attribute, fabricated evidence, fence violations, terminal-stage locks.
- Engine tests stub `llm.client.call_json` exactly as existing tests stub
  `call_messages`; `reset_for_tests` already exists in `app/llm/client.py`.
- Observability (carried from `specs/002 §3.2`): log counters for extraction
  rejections per task (prompt-rot detector), triage/extraction latency, and per-user
  sync lag; `task_events` itself is the audit log.
