# Phase 4 — Bucket Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Spec: `specs/004_vision_arch/chosen-architecture.md` §3 Phase 4, D2, §1 item 8, §4.7.
> Plan stamped at commit `05e372b` on branch `main`.
> NOTE: the spec's §5.2 sketch calls this "migration 0008", but 0008 was consumed by Phase 2B (`0008_pending_reason.py`) — this phase ships as **migration 0009**.

**Goal:** Buckets become the degenerate task: an ID-preserving migration folds `buckets` rows into `tasks(kind='bucket')`, `inbox_threads.bucket_id` retargets with zero row updates, `/api/buckets` survives one release as a shim, the bucket modals / `reclassify_user_inbox` / `preview_cache` bucket paths are deleted, and bucket creation goes through the NewTaskWizard with the schema step skipped.

**Architecture:** Two compatibility seams keep the diff small: server-side, `bucket_repo`'s five functions keep their exact signatures but return `Task` rows (kind='bucket') — so `gmail_sync`, triage, and the `/api/buckets` shim keep working unchanged; client-side, `useBuckets` keeps its exact external shape (`{buckets, byId, customBuckets, loading, refresh, create, rename, softDelete}`) but its internals move to the task routes — so `FilterByBucketDropdown`, `BucketPill`, HUD bucket cards, and `ThreadsPanel` need zero edits. New-bucket reclassification is replaced by a kind-aware `backfill_task` branch (spec §4.7: reclassify is deleted in favor of backfill).

**Tech Stack:** existing (uv / bun; no new dependencies).

## Global Constraints

- All prior conventions hold: `uv`/`bun` only; NEVER read `.env`; repo functions never commit; read helpers never flush; publish-after-commit; TDD server-side (suite baseline **465 passed** — will DROP when bucket/classify test files are deleted/rewritten; the invariant is 0 failures, not the count); client verification = `bun run build` + `bun x tsc --noEmit`; worktrees lack `.env` → settings assertions via resolved `get_settings()`.
- Migration tests run alembic on SQLite → **dialect-guard PG-only DDL** (`op.get_bind().dialect.name == "postgresql"`), pattern per `0006_data_floor.py`.
- **ID preservation is the invariant**: `tasks.id` == old `buckets.id` for every migrated row; `inbox_threads.bucket_id` values are NEVER rewritten. Default buckets stay shared rows: `user_id IS NULL`, visible to every user, immutable via API.
- Task model was pre-staged for this (`db/models.py:132-155`): `user_id` nullable ("reserved for Phase-4 default classify-tasks"), `kind ∈ {'tracker','bucket'}`, `state_schema` NULL = classify-only. Do not alter these semantics.
- Bucket-kind tasks NEVER get `task_thread_links` / entities / events — exactly-one-per-thread semantics keyed off `kind` directly (spec §4.1). Triage's dual-write stays: `bucket_id` for the bucket pick, links for trackers only.
- Beat stays SINGLE REPLICA. No new Railway services.
- Commit per task, `type(scope): summary`, no attribution lines.

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `server/migrations/versions/0009_bucket_unification.py` | create | data copy, FK retarget (PG), drop `buckets` |
| `server/app/db/models.py` | modify | delete `Bucket`; `InboxThread.bucket_id` → FK `tasks.id` |
| `server/app/inbox/bucket_repo.py` | rewrite | same signatures, task-backed (the server compat seam) |
| `server/app/task_engine/repo.py` | modify | `list_active_buckets` helper |
| `server/app/api/tasks.py` | modify | `kind` on create body + list query param; bucket-row extras |
| `server/app/workers/task_engine_tasks.py` | modify | kind-aware backfill branch (bucket = re-triage, no extraction) |
| `server/app/workers/tasks.py` | modify | DELETE `reclassify_user_inbox`/`_reclassify_all`/`draft_preview_bucket`; keep `_score_all`/`_read_candidates` |
| `server/app/inbox/preview_cache.py` | delete | bucket draft preview cache (task drafts use `draft_cache.py`) |
| `server/app/api/buckets.py` | rewrite | one-release SHIM (CRUD only; preview routes deleted) |
| `server/app/llm/classify.py` + prompts | modify | delete legacy `classify()`; type hints Bucket→Task; keep `classify_thread.py` (triage's substrate) |
| `server/tests/` | rewrite/delete | `test_classify.py` deleted; `test_bucket_repo`/`test_buckets_api` rewritten; Bucket fixtures → Task(kind='bucket') sweep |
| `client/src/lib/api.ts` | modify | `getTasks(kind?)`, `createTask` kind+nullable schema; delete bucket fetchers + preview SSE type |
| `client/src/pages/buckets/useBuckets.tsx` | rewrite | same external shape, task-route internals (the client compat seam) |
| `client/src/pages/task/NewTaskWizard.tsx` | modify | `kind='bucket'` mode (schema step skipped) |
| `client/src/pages/inbox/InboxPage.tsx` | modify | wizard replaces NewBucketModal; delete `createWithWatchdog` |
| `client/src/pages/buckets/NewBucketModal.tsx` | delete | superseded by wizard |
| `reference/*` | modify (last) | re-index + stamps |

---

### Task 1: The storage flip — migration 0009, models, task-backed bucket_repo

**Files:** Create `server/migrations/versions/0009_bucket_unification.py`; Modify `server/app/db/models.py`, `server/app/inbox/bucket_repo.py`, `server/app/task_engine/repo.py`; Test: extend `server/tests/test_migrations.py` (follow its existing alembic-on-SQLite pattern), rewrite `server/tests/test_bucket_repo.py`, mechanical fixture sweep in `server/tests/test_triage.py`, `test_llm_prompts.py`, `test_buckets_api.py`, `test_task_engine_tasks.py`, `test_task_repo.py` (any `Bucket(...)` construction → `Task(kind='bucket', ...)` with equivalent fields; `test_classify.py` gets a minimal fixture swap here just to keep the suite green — it is deleted in Task 3).

**Interfaces produced:**
- `task_engine/repo.py`: `list_active_buckets(db, *, user_id: str) -> list[Task]` — kind='bucket', not deleted, `(Task.user_id.is_(None)) | (Task.user_id == user_id)`, ordered `Task.name.asc()`. Read-only, never flushes.
- `bucket_repo` keeps its five signatures verbatim but returns/accepts `Task` rows: `list_active` delegates to `list_active_buckets`; `get_by_id` → `db.get(Task, bucket_id)` returning None for non-bucket kinds (guard `row.kind == 'bucket'`); `create_custom(db, *, user_id, name, criteria)` → delegates to `task_repo.create_task(db, user_id=user_id, name=name, goal="", criteria=criteria, state_schema=None, kind="bucket")`; `rename`/`soft_delete` mutate the Task row (soft_delete sets `is_deleted=True`). Module docstring updated: "task-backed since Phase 4; buckets are tasks(kind='bucket')". The `formulate_criteria` re-export stays.
- Duck-type contract consumers rely on: `.id`, `.name`, `.criteria`, `.user_id`, `.is_deleted` — identical on Task; `is_default` == `user_id is None` (unchanged predicate).

**Migration 0009** (`down_revision = "0008"`):
- Data copy (raw SQL, both dialects):
  ```sql
  INSERT INTO tasks (id, user_id, kind, name, goal, criteria, state_schema,
                     status, version, is_deleted, created_at)
  SELECT id, user_id, 'bucket', name, '', criteria, NULL,
         'active', 1, is_deleted, CURRENT_TIMESTAMP
  FROM buckets
  ```
- FK retarget, PG only (dialect guard): `op.drop_constraint("inbox_threads_bucket_id_fkey", "inbox_threads", type_="foreignkey")` then `op.create_foreign_key("inbox_threads_bucket_id_fkey", "inbox_threads", "tasks", ["bucket_id"], ["id"])`. On SQLite: skip with a comment — SQLite tests don't enforce FKs and `Base.metadata.create_all` (models) is the source of truth there; a table rebuild is not worth the risk. VERIFY the PG constraint name first: `git grep bucket_id server/migrations/versions/0002_inbox.py` — if the FK was created unnamed, PG autonames it `inbox_threads_bucket_id_fkey`; if the migration named it differently, use that name.
- `op.drop_table("buckets")` (both dialects; on SQLite this is legal with FK pragma off).
- Downgrade: recreate `buckets` from the 0003 shape, copy `kind='bucket'` task rows back, retarget the FK on PG, delete `kind='bucket'` rows from `tasks`.
- **models.py**: delete `class Bucket`; `InboxThread.bucket_id` → `ForeignKey("tasks.id")`. Update the `Task` docstring's "(or, in Phase 4, ...)" phrasing to present tense.

- [ ] TDD (migration test, alembic-on-SQLite pattern): seed `buckets` rows (one default `user_id=NULL`, one custom, one soft-deleted custom) + an `inbox_threads` row pointing at the custom bucket at revision 0008 → upgrade to 0009 → assert: task rows exist with SAME ids, kind='bucket', criteria carried, `is_deleted` carried, `state_schema` NULL; the thread's `bucket_id` value is unchanged; `buckets` table gone.
- [ ] TDD (`test_bucket_repo.py` rewrite): `list_active` returns defaults + own customs, excludes deleted + other users' + tracker-kind tasks, name-ordered; `get_by_id` returns None for a tracker-kind id; `create_custom` mints a Task with kind='bucket', schema NULL, version 1; rename/soft_delete mutate.
- [ ] Fixture sweep: every remaining `Bucket(` in `server/tests/` → `Task(id=..., user_id=..., kind="bucket", name=..., criteria=..., is_deleted=...)` (fill `goal=""`, `status="active"`, `version=1`, `created_at=<any tz-aware datetime>` as the model requires). Full suite green, 0 failures.
- [ ] Commit: `feat(db): unify buckets into tasks(kind='bucket') — id-preserving migration 0009`

---

### Task 2: Kind-aware creation + bucket backfill; reclassify deleted

**Files:** Modify `server/app/api/tasks.py`, `server/app/workers/task_engine_tasks.py`, `server/app/workers/tasks.py`, `server/app/api/buckets.py` (POST route only — full shim rewrite is Task 3); Test: extend `server/tests/test_tasks_api.py`, `test_task_engine_tasks.py`; update `test_tasks.py` (reclassify tests deleted).

**Interfaces:**
- `_CreateTaskBody` gains `kind: Literal["tracker", "bucket"] = "tracker"`; `state_schema: dict | None = None`. Validation in `create_task`: kind='tracker' → `state_schema` required (422 `"state_schema is required for tracker tasks"`); kind='bucket' → `state_schema` must be None (422 `"bucket tasks cannot have a state_schema"`). Pass `kind=body.kind` through to `task_repo.create_task` (the repo already takes `kind`). The existing backfill enqueue on create fires for both kinds.
- `backfill_task` branches on `task.kind`:
  - `'tracker'`: existing behavior, untouched.
  - `'bucket'`: candidates via the same FTS-probe prefilter (empty probes → the same recent-window fallback the tracker path/`_read_candidates` uses — read the existing candidate code and mirror it); then per batch call `classify.triage(parsed_list, buckets=list_active_buckets(db, user_id=uid), trackers=[], current_bucket_ids=current, user_id=uid, task_id=task.id)` — the FULL bucket set (new bucket included) with each thread's current `bucket_id` as the stability hint, so the LLM only churns threads that genuinely fit the new bucket better (same rationale as the old `_reclassify_all`); write `thread_row.bucket_id = new_bucket` only when the pick differs (mirror `_reclassify_all`'s exact write discipline), commit per batch, publish `threads_updated` for changed ids + `task_backfill_progress` on the same cadence as the tracker path. NO extraction, NO `task_thread_links` writes.
- DELETE from `workers/tasks.py`: `reclassify_user_inbox`, `_reclassify_all`, and their tests. KEEP `_score_all`, `_read_candidates`, `_publish` (the task-draft flow imports them — verify `task_engine_tasks.py:315` still resolves).
- `api/buckets.py` POST: replace `tasks.reclassify_user_inbox.apply_async(...)` with creating via `bucket_repo.create_custom` + enqueueing `backfill_task` for the new task id with `keyword_probes=[]` (match `backfill_task`'s actual signature — read it).

- [ ] TDD: create-task kind validation (both 422 paths + happy bucket create with NULL schema); bucket backfill writes changed `bucket_id`s and skips unchanged (stability hint passed through — assert via monkeypatched `triage` capturing `current_bucket_ids`); bucket backfill produces zero `task_thread_links`/`task_events`/extraction enqueues; empty-probe candidate fallback; POST /api/buckets enqueues backfill (not reclassify). Full suite green.
- [ ] Commit: `feat(engine): kind-aware create + bucket backfill replaces reclassify`

---

### Task 3: Shim consolidation, preview/classify deletion, task-list kind param

**Files:** Rewrite `server/app/api/buckets.py`; Delete `server/app/inbox/preview_cache.py`, `server/tests/test_classify.py`; Modify `server/app/workers/tasks.py` (delete `draft_preview_bucket`), `server/app/api/tasks.py` (list route), `server/app/llm/classify.py` (delete `classify()`; retype `triage` params `list[Task]`), `server/app/llm/prompts/classify_thread.py` + `triage_thread.py` (type hints only — `classify_thread.py` module SURVIVES; `triage_thread` builds on it), `server/tests/test_triage.py` (drop/rewrite the two classify-equivalence tests to not import `classify()`), rewrite `server/tests/test_buckets_api.py` as shim tests.

**Shim contract** (`api/buckets.py`, top-of-file comment: `# SHIM — Phase 4 back-compat for one release. Backed by tasks(kind='bucket'). Delete in Phase 5.`):
- `GET /api/buckets` → `bucket_repo.list_active`, serialized `{id, name, criteria, is_default: row.user_id is None}` (old shape exactly).
- `POST /api/buckets` (201) → as wired in Task 2.
- `PATCH /api/buckets/{id}` / `DELETE /api/buckets/{id}` → keep the exact old policy ladder: missing/deleted → 404, `user_id is None` → 403 `"cannot modify default bucket"` / `"cannot delete default bucket"`, other-user → 403, DELETE idempotent on already-deleted. (`_load_owned_or_403` survives nearly verbatim against Task rows.)
- `POST /api/buckets/draft/preview` + `GET /api/buckets/draft/preview/{id}`: DELETED (spec: preview bucket paths deleted; the new client doesn't call them — a stale pre-deploy tab gets 404 and `NewBucketModal`'s existing `gone` handling console-warns).
- `GET /api/tasks` gains `kind: str | None = Query(default=None)`: `None` → `task_repo.list_tasks(db, user_id=user.id, kind="tracker")` (explicit — keeps the HUD grid/feeds tracker-only now that buckets share the table); `"bucket"` → `list_active_buckets` (defaults included); `"tracker"` → same as None; other values → 422. Bucket-kind rows in the response additionally carry `"criteria"` and `"is_default"` (kind-conditional fields, documented in the serializer — trackers don't pay the payload).
- Also delete: the `bucket_draft_preview` publish in `workers/tasks.py`, and `classify()` in `llm/classify.py` (triage is the only caller path left; `test_triage`'s two equivalence tests either delete or re-derive without `classify`).

- [ ] TDD (shim): list shape incl. `is_default`; default-bucket 403s on PATCH/DELETE; other-user 403; DELETE idempotency; preview routes 404. TDD (tasks list): no-param excludes bucket-kind; `?kind=bucket` includes defaults + `criteria`/`is_default` keys; `?kind=whatever` 422. Full suite green (count will drop — assert 0 failures).
- [ ] Commit: `refactor(api): /api/buckets becomes a one-release shim; preview + legacy classify deleted`

---

### Task 4: Client data layer — api.ts + task-backed useBuckets

**Files:** Modify `client/src/lib/api.ts`, `client/src/lib/sse.ts`, `client/src/pages/buckets/useBuckets.tsx`.

- `api.ts`: `getTasks(opts?: { kind?: 'bucket' })` (appends `?kind=bucket`; existing no-arg call sites unchanged); `createTask` body gains `kind?: 'tracker' | 'bucket'` and `state_schema: TaskStateSchema | null`; task list item type gains `criteria?: string; is_default?: boolean` (kind-conditional, optional). DELETE `getBuckets`, `createBucket`, `patchBucket`, `deleteBucket`, `postBucketDraftPreview`, `getBucketDraftPreview` and the `bucket_draft_preview` member of `SseDataEvent` in `sse.ts`. KEEP the `Bucket` type (now the client-side mapped shape) and `BucketExampleIn` (reused by `createTask`).
- `useBuckets.tsx`: same file, same exported shape `{buckets, byId, customBuckets, loading, refresh, create, rename, softDelete}`. Internals: `refresh` → `getTasks({kind:'bucket'})` mapped to `Bucket` (`{id, name, criteria: item.criteria ?? '', is_default: item.is_default ?? false}`); `create(body)` → `createTask({...body, goal: body.description, kind:'bucket', state_schema: null, keyword_probes: []})` then refresh (NOTE: this `create` survives only for the shim window if anything still calls it — Task 5 moves creation into the wizard; if after Task 5 nothing calls `create`, delete it and note in the report); `rename` → `patchTask(id, {name})`; `softDelete` → `deleteTask(id)`; each followed by `refresh()` as today. Downstream consumers (`FilterByBucketDropdown`, `BucketPill`/`InboxList`, HUD bucket cards, `ThreadsPanel`, `useInbox.filteredIdLayer`) need ZERO edits — verify by grep that none import the deleted fetchers.
- [ ] Build + tsc clean. Commit: `feat(client): bucket data layer moves to task routes`

---

### Task 5: Wizard bucket mode; InboxPage swap; NewBucketModal deleted

**Files:** Modify `client/src/pages/task/NewTaskWizard.tsx`, `client/src/pages/inbox/InboxPage.tsx`; Delete `client/src/pages/buckets/NewBucketModal.tsx`.

- `NewTaskWizard` gains `kind?: 'tracker' | 'bucket'` (default `'tracker'`) and `onCreated?: (taskId: string) => void`:
  - bucket mode: the review step renders WITHOUT `<SchemaEditor>` (`stateSchema` stays null; `createTask` payload sends `state_schema: null, kind: 'bucket'`); copy tweaks: form-step prompt "What should this bucket catch?", review header "New bucket"; the `'creating'` step works as-is IF `TasksProvider` records `task_backfill_progress` for task ids not in its list — VERIFY that first; if it filters to known tasks, subscribe to the SSE singleton directly inside the wizard for bucket mode (same pattern the provider uses). On completion: call `onCreated(taskId)` and `onClose()` — do NOT `navigate('/tasks/:id')` in bucket mode (there is no task page for buckets).
  - tracker mode: behavior byte-identical to today (probe with a careful read of the diff — the mode split must not touch the tracker path).
- `InboxPage`: `showNew` now renders `<NewTaskWizard kind="bucket" onCreated={() => buckets.refresh()} onClose={...} />`; DELETE `createWithWatchdog` (the 60s/150s resync watchdog existed to mask the fire-and-forget reclassify — bucket backfill publishes `threads_updated` deterministically); delete the `NewBucketModal` import/render.
- [ ] Build + tsc clean; self-review the mode split for tracker-path regressions. Commit: `feat(client): bucket creation through the task wizard, schema step skipped`

---

### Task 6: Reference docs

- [ ] Update `TASKS_INDEX.md` (kind='bucket' semantics, `list_active_buckets`, create-kind validation, bucket backfill branch, shim pointer), `INBOX_SYNC_INDEX.md` + `WORKERS_INDEX.md` (reclassify/draft_preview deleted; triage loads buckets from tasks table; backfill's bucket branch), `CLIENT_INDEX.md` (useBuckets task-backed, NewBucketModal gone, wizard kind prop, deleted fetchers/SSE event). MANIFEST rows + stamps. Every claim verified against code; stamp to the final code commit. Commit: `docs(reference): re-index for phase 4 bucket unification`
- [ ] Final: suite green; build clean.

---

### Task 7 (manual acceptance — coordinator + user)

Dev stack (migration 0009 runs against the local PG — existing bucket rows must survive with the same ids): existing buckets still show in the inbox filter + HUD cards with correct counts; thread pills unchanged (FK retarget invisible); create a bucket via the wizard (no schema step) → backfill re-triages candidates and the inbox updates; rename + delete a custom bucket; defaults show no rename/delete affordances and the shim returns 403 if forced; a tracker create via the wizard still works end-to-end (mode-split regression check); `GET /api/buckets` (shim) returns the old shape.
