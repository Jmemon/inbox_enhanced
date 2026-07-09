<!-- stamp: c5a7fef (main) | 2026-07-09 | Jobs surface design — brainstormed + user-ratified -->

# 005 — Jobs Surface

Replace the wizard's auto-opening, screen-blocking waiting popups with a
user-initiated jobs surface: a header chip + slide-over panel where ongoing
task/bucket creation work shows live progress, and a **blue dot** marks jobs
that are ready for user interaction. Nothing ever pops up on its own again.

Origin: Phase 4 manual-gate feedback (2026-07-09). The proximate bug — a
bucket-creation popup stranded forever after an SSE blip — is fixed
structurally here: job state becomes **persisted and pollable** instead of
living only in fire-and-forget SSE events.

Decisions ratified with the user:
1. **Job scope = the full creation flow** (both the goal→draft wait and the
   post-create backfill are stages of one job), plus bucket-delete re-triage
   as a second job kind. Not backfill-only; not a speculative generic
   framework (Phase 5 actions can join later via `kind`).
2. **Durability = Postgres `jobs` table** (migration 0010). Redis-TTL
   rejected: the sidebar must survive restarts/expiry, and drafts must stop
   expiring after 10 minutes.
3. **Placement = header chip + right slide-over panel.** Persistent rail and
   corner dock rejected (idle screen cost / poor stacking).

---

## 1. Jobs domain (server)

### 1.1 Table (migration 0010, `jobs`)

| column | type | notes |
|--------|------|-------|
| `id` | String(36) PK | uuid4 hex |
| `user_id` | String(36) FK users.id, NOT NULL, indexed | jobs are always user-owned (no NULL-user jobs) |
| `kind` | String(32) NOT NULL | `'creation'` \| `'delete_retriage'` |
| `task_kind` | String(16) nullable | `'tracker'` \| `'bucket'` — creation jobs only |
| `stage` | String(32) NOT NULL | see state machines below |
| `needs_user` | Boolean NOT NULL default false | true only in `draft_ready`; denormalized for the chip query |
| `payload` | JSON/JSONB nullable | the proposed draft: `{name, description, criteria, state_schema, keyword_probes, positives, near_misses}` — written by the propose worker, read by the review step |
| `task_id` | String(36) nullable | set at confirm-time (creation) or at enqueue-time (delete_retriage = the deleted bucket's id) |
| `goal` | Text NOT NULL default '' | the user's original goal text (creation); display name fallback |
| `scanned` / `matched` / `total` | Integer NOT NULL default 0 | progress counters, updated per batch |
| `error` | Text nullable | populated on `failed` |
| `created_at` / `updated_at` | DateTime(timezone=True) NOT NULL | |
| `dismissed_at` | DateTime(timezone=True) nullable | user dismissal; dismissed jobs leave the panel |

JSON `with_variant(JSONB, "postgresql")` per repo convention; migration
dialect-guards any PG-only DDL (none expected beyond JSONB variant).

### 1.2 Stage machines

- **creation**: `proposing` → `draft_ready` (needs_user=true) → `backfilling`
  → `done` | `failed` (from any stage; `error` set) | `dismissed`
  (user-initiated, any non-terminal stage = cancel-intent: a dismissed
  `draft_ready` job simply never confirms; a dismissed `backfilling` job
  keeps running server-side but disappears from the UI — we do NOT implement
  worker cancellation).
- **delete_retriage**: `running` → `done` | `failed`. `needs_user` is never
  true; it exists to show progress and completion.

### 1.3 Repo (`task_engine/jobs_repo.py`, never commits)

`create_job`, `get_owned_job`, `list_jobs(db, *, user_id, active_only)`
(active = not dismissed AND (non-terminal OR terminal within 7 days),
newest first), `update_stage`, `update_progress`, `set_payload`,
`mark_failed`, `dismiss`. Write helpers may flush; reads never do.

### 1.4 HTTP surface (`api/jobs.py`, all `Depends(get_current_user)`)

- `POST /api/jobs` `{goal, task_kind}` → 202 `{job}` — creates the row
  (`proposing`) and enqueues the propose worker. Replaces
  `POST /api/tasks/draft` for the wizard (old draft routes + the Redis
  `draft_cache` **retire for the creation flow**; drafts now live in
  `jobs.payload` and never expire).
- `GET /api/jobs?active=1` → `{jobs: [...]}`; `GET /api/jobs/{id}` → `{job}`.
  This is the always-works poll path — the structural fix for the stranded
  popup.
- `POST /api/jobs/{id}/confirm` `{name, description, state_schema|null,
  keyword_probes, confirmed_positives, confirmed_negatives}` — only legal in
  `draft_ready`; creates the task through the existing kind-aware create
  internals (same 422 rules), sets `task_id`, `stage=backfilling`, enqueues
  the backfill. 409 on wrong stage.
- `POST /api/jobs/{id}/dismiss` → 204, idempotent.
- `POST /api/tasks` (direct create) remains unchanged for API/test use; it
  enqueues a backfill with NO job row, so its progress is simply absent
  from the panel. The wizard no longer calls it.

### 1.5 Workers

- Propose worker writes the proposal into `jobs.payload`, sets
  `draft_ready`/`needs_user`, publishes the SSE nudge. Existing 2-call retry
  cap and failure handling carry over; on exhaustion → `failed` + `error`.
- `backfill_task` (both kinds) gains a `job_id` arg: per batch it updates
  `scanned/matched/total` on the job row (commit-then-publish), and sets the
  terminal stage. A top-level try/except marks the job `failed` with the
  error — closing the ledgered "raise before terminal publish hangs the
  client" latent for good.
- **Delete re-triage**: deleting a bucket-kind task now (a) snapshots the
  ids of threads whose `bucket_id` pointed at it, (b) creates a
  `delete_retriage` job (`total` = count), (c) enqueues a worker that
  re-triages those threads against the remaining bucket set (batched;
  mirrors `_run_bucket_backfill`'s write discipline incl. the optimistic
  skip-if-moved guard; threads the LLM can't place go/stay unclassified),
  publishing `threads_updated` + job progress as it goes.

### 1.6 SSE

One new event: `job_updated {job_id}` — a pure nudge; the client refetches
jobs (events carry ids, never rows, per convention). Published after every
job-row commit (stage changes and progress ticks). The old
`task_draft_ready` event retires with the draft routes;
`task_backfill_progress` remains for the TaskDetail live board but the
jobs surface does not depend on it.

## 2. Client

### 2.1 `JobsProvider` (`state/JobsProvider.tsx`, mounted in AppShell)

Fetches `GET /api/jobs?active=1` on mount; refetches on `job_updated`; runs
a 15s polling interval **only while any job is non-terminal** (belt to
SSE's suspenders — a dropped connection now only delays updates until the
next poll). Exposes `{jobs, refresh, startCreation, confirmJob, dismissJob}`.

### 2.2 Header `JobsChip` (in AppShell's header)

- Zero non-dismissed active jobs → renders nothing.
- Running jobs → compact chip `[◌ Jobs N]` with a spinner.
- Any `needs_user` → blue dot on the chip.
- Click toggles the slide-over panel.

### 2.3 `JobsPanel` (right slide-over, overlays content, never blocks it)

Job cards, newest first: title (task name once known, else goal), stage
line, progress bar with `scanned/total` + matched count while
`backfilling`/`running`, and stage-appropriate actions:
- `draft_ready` → blue dot + **[Review]** → opens the wizard at the review
  step, seeded from `payload`.
- `done` creation (tracker) → link to `/tasks/{task_id}`; (bucket) →
  "bucket live" + dismiss.
- `done` delete_retriage → "N threads reclassified" + dismiss.
- `failed` → error text + dismiss.
Terminal jobs older than 7 days are dropped by `list_jobs`; a beat-driven
hard delete by age is a later nicety, not in scope.

### 2.4 Wizard restructure (`NewTaskWizard`)

Two user-initiated moments; the modal NEVER opens or persists on its own:
1. **Start**: form step (goal) → `startCreation` → **modal closes
   immediately**; the chip starts spinning. The old in-modal `pending` wait
   is deleted.
2. **Review**: clicking a `draft_ready` job opens the review step seeded
   from `jobs.payload` (name/description/examples; SchemaEditor for
   trackers only, unchanged skip for buckets) → confirm → `confirmJob` →
   **modal closes immediately**; backfill progress lives in the panel. The
   old in-modal `creating` wait is deleted.
Create-error (422 on confirm) renders inline on the review step as today.

### 2.5 Small fix riding along

`ViewBucketsModal`: the delete-confirm renders inline directly under the
bucket section header (not at the bottom of the list), so no scrolling to
confirm.

## 3. Pipeline stage ordering (SchemaEditor)

Stages are ordered — the EPS `pipeline.stages` array drives board columns
and backward-move detection — so the editor now says so visually:
- Stage chips render with **→ arrows** between them.
- Chips are **drag-to-reorder** (pre-creation, in the wizard). Terminal
  states remain a separate unordered group with no arrows.
- Post-creation schema edits stay additive-only per the 004 spec; the
  implementation plan pins what the PATCH validator actually permits
  (mid-pipeline insertion vs end-append) and the editor offers exactly
  that — no UI affordance the validator would reject.

## 4. Out of scope / deferred

- Retry button on failed jobs (dismiss-and-recreate is cheap).
- Worker-side cancellation of dismissed running jobs.
- Phase 5 action jobs (the `kind` column leaves room).
- Beat-driven hard deletion of ancient job rows.
- Migrating `TaskDetail`'s live board progress off `task_backfill_progress`.

## 5. Testing invariants

- Jobs repo: never commits; `list_jobs` scoping (user A never sees B's jobs
  — same security posture as the feeds), active-window semantics.
- API: stage-gating (confirm only from `draft_ready` → 409 otherwise),
  ownership 404s, dismiss idempotency, kind-aware confirm 422 parity with
  `POST /api/tasks`.
- Workers: propose failure → `failed`+`error`; backfill exception →
  `failed` (no more silent hangs); progress rows monotonically updated;
  delete_retriage rewrites only threads that still point at the deleted
  bucket (optimistic guard) and never touches links/events.
- Client: build + tsc; reviewer probes on the 15s poll gating (no interval
  leak when all jobs terminal) and the wizard's two-entry restructure
  (tracker path regression-checked).
