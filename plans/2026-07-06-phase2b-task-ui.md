# Phase 2B — Task UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Spec: `specs/004_vision_arch/chosen-architecture.md` §3 Phase 2 (client half) + §4.6 + §6.
> Companion: `plans/2026-07-06-phase2a-task-engine.md` (shipped — the API this builds against). Route/payload shapes below were read from the MERGED `server/app/api/tasks.py` serializers, not from the plan.
> Plan stamped at commit `c70b163` on branch `main`.

**Goal:** Make trackers usable: the creation wizard (goal → LLM-proposed schema/criteria → edit/confirm → live backfill), the `/tasks/:id` page (pipeline board, review tray, threads panel, full correction loop), HUD task cards — plus the two server-side carry-overs the 2A final review mandated: the §4.6 correction→criteria learning loop and pending-reason provenance for the review tray.

**Architecture:** Client state follows the AppShell-provider rule (spec §6): a `TasksProvider` beside `InboxProvider` owns the task list + per-task detail cache and routes `task_*` SSE events (version-gap OR pending-count-change refetch — the 2A ledger's no-bump publishes make version alone insufficient). The wizard clones `NewBucketModal`'s `form|pending|review` machine + `appliedRef` SSE-or-poll idempotency. No new client dependencies (stage moves via per-card menu, not a drag library). Two server tasks land first (migration 0008 + learning loop) so the UI renders real pending reasons.

**Tech Stack:** React 19 / Vite / TS (bun) + existing react-router; FastAPI/SQLAlchemy/Celery (server tasks).

## Global Constraints

- `uv` for Python, `bun` for JS; NO new client dependencies. NEVER read `.env`; worktrees lack `.env` → settings-dependent test assertions use resolved `get_settings()` values, never default literals.
- Server: repo functions never commit; read helpers never flush (write-path may); publish-after-commit; TDD; suite baseline **367 passed**.
- Client: no test runner — verification is `cd client && bun run build 2>&1 | tail -3` clean per task + reviewer probes; nothing under `server/app/static/` committed.
- Providers: pages consume stores, they don't own them; hooks composed in providers stay unmodified where possible.
- 2A client notes (binding): `task_updated` publishes from reject/attach/DELETE do NOT bump version — client refetches on `version > known || pending_count !== known`; a `task_updated` for an id not in the list triggers a list refetch (covers create-from-another-tab); detail refetch returning 404 removes the task locally (covers DELETE). Thread lists from the API are unordered — sort client-side by `recent_message.internal_date` desc. `GET /api/tasks` is unfiltered by `kind` (all tasks are trackers until Phase 4 — render as-is).
- API payload shapes (from `api/tasks.py`, verbatim): task `{id,name,goal,kind,status,version}`; list items/detail add `summary {entities, pending_reviews, last_event_at}`; detail adds `state_schema`; entity `{id, entity_key, display_name, state, updated_at}`; event `{id, field, old_value, new_value, evidence_quote, confidence, origin, status, thread_id, message_id, gmail_message_id, entity_id, created_at}` (+ `pending_reason`, `proposed_entity` after Task 1).
- Commit per task, `type(scope): summary`, no attribution lines.

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `server/migrations/versions/0008_pending_reason.py` + `models.py` | create/modify | `task_events.{pending_reason, proposed_entity}` |
| `server/app/task_engine/transitions.py` | modify | write reasons; float-confidence clamp |
| `server/app/api/tasks.py` | modify | serialize new fields; detach auto-rejects pendings; `add_example` |
| `server/app/task_engine/criteria.py` | modify | `append_example`, `cap_examples` (§4.6) |
| `server/app/llm/prompts/extract_transition.py` + `engine.py` + `repo.py` | modify | correction exemplars in the extraction prompt |
| `client/src/lib/api.ts`, `client/src/lib/sse.ts` | modify | task types/fetchers; SSE union |
| `client/src/state/TasksProvider.tsx` | create | task store + SSE routing |
| `client/src/AppShell.tsx` | modify | mount TasksProvider |
| `client/src/pages/task/NewTaskWizard.tsx`, `SchemaEditor.tsx` | create | creation flow |
| `client/src/pages/task/TaskDetail.tsx`, `PipelineBoard.tsx`, `EntityDrawer.tsx`, `ReviewFeed.tsx`, `ThreadsPanel.tsx` | create | /tasks/:id |
| `client/src/App.tsx` | modify | `/tasks/:taskId` route |
| `client/src/pages/hud/HudPage.tsx` | modify | task cards grid + New Task |
| `reference/{TASKS_INDEX,CLIENT_INDEX,MANIFEST}.md` | modify (last task) | re-index + stamps |

Ship gate (spec): the agent-3 worked example (`specs/004_vision_arch/agent-3-task-model.md` §6) end to end against a real Gmail account, including both corrections — run manually by the coordinator + user at the end.

---

### Task 1: Pending provenance + review-tray hardening (server)

**Files:** Modify `server/app/db/models.py` (TaskEvent), Create `server/migrations/versions/0008_pending_reason.py`, Modify `server/app/task_engine/transitions.py`, `server/app/llm/prompts/extract_transition.py` (confidence clamp in parse), `server/app/api/tasks.py` (serializer + detach); Tests: `test_migration_0008.py`, extend `test_transitions.py`, `test_extract_prompt.py`, `test_tasks_api.py`.

**Interfaces produced:**
- `TaskEvent.pending_reason: Mapped[str | None] = mapped_column(String(32))` — vocabulary: `near_duplicate_entity | backward_move | terminal_locked | fence_blocked | low_confidence`; `TaskEvent.proposed_entity: Mapped[str | None] = mapped_column(String(255))` (the LLM's verbatim entity string, written on `near_duplicate_entity` pendings so the tray can render "LLM said 'Stripewise Corp', closest match 'stripe'"). Migration 0008 (`down_revision="0007_tasks"`): two nullable columns, plain add/drop, no dialect guards.
- `transitions.validate_and_stage`: every `pending_review` event carries the reason for the FIRST guard that forced pending (steps 2/3/5 set `forced_pending` — record which; step-8 low-confidence pendings get `low_confidence`); `near_duplicate_entity` events also carry `proposed_entity`.
- `extract_transition.parse_response`: float confidences are clamped to int (parity with triage — a Sonnet `82.5` must not discard a real transition).
- `api/tasks.py`: `_serialize_event` adds both fields; `DELETE /api/tasks/{id}/threads/{thread_id}` additionally flips that thread's `pending_review` events to `rejected` (a detached thread's pendings must not be approvable later).

**Steps:**
- [ ] Failing tests: migration adds/drops columns; each forced-pending path writes its reason (parametrize the five); near-dup pending carries proposed_entity; float confidence `82.5` applies (≥75) instead of dropping; detach rejects that thread's pendings (seed one, detach, assert `rejected`) while other threads' pendings survive; serializer exposes both fields.
- [ ] Implement → pass; full suite green (`cd server && uv run pytest -q 2>&1 | tail -2`).
- [ ] Commit: `feat(task-engine): pending reasons + proposed entity; detach rejects pendings; float confidence clamp`

---

### Task 2: §4.6 learning loop (server)

**Files:** Modify `server/app/task_engine/criteria.py`, `server/app/api/tasks.py` (attach/detach), `server/app/llm/prompts/extract_transition.py`, `server/app/task_engine/engine.py`, `server/app/task_engine/repo.py`; Tests: extend `test_task_repo.py` (or new `test_criteria.py`), `test_tasks_api.py`, `test_extract_prompt.py`, `test_task_engine_tasks.py`.

**Interfaces produced:**

```python
# criteria.py additions
EXAMPLE_CAP = 30  # FIFO across positives+nearmisses combined

def append_example(criteria: str, *, example: dict, tag: str) -> str:
    """tag ∈ {'positive','nearmiss'}. Renders ONE block in formulate_criteria's
    exact grammar (From/To/Subject/snippet/Why) and appends it. If the criteria
    lacks an 'Example cases:' section (legacy/empty), adds one. Then applies
    cap_examples."""

def cap_examples(criteria: str, *, cap: int = EXAMPLE_CAP) -> str:
    """Parses <positive>/<nearmiss> blocks by tag regex (non-greedy, DOTALL),
    keeps the description + section header intact, drops OLDEST blocks
    (document order) beyond cap, preserving relative order of survivors."""

# repo.py addition
def recent_user_events(db, *, task_id, limit=5) -> list[TaskEvent]
    # origin='user', status='applied', newest first
```

- `POST /api/tasks/{id}/threads` body gains `add_example: bool = True`; `DELETE .../threads/{thread_id}` gains query param `add_example: bool = True`. When true: build the example dict from the thread's recent message (`sender=from_addr, subject=thread.subject, snippet=body_preview or '', rationale='user attached this thread to the task' / 'user detached this thread from the task'`), `task.criteria = criteria_mod.append_example(...)` with tag `positive` (attach) / `nearmiss` (detach), same transaction, version bump already happens via the route.
- Extraction prompt: `build_user_message` gains `user_corrections: list | None = None`; rendered as a `Corrections the user has made (respect these):` section — one line per event: `- {entity display or key}: user set {field} to "{new_value}"` . `engine.extract_for_pair` passes `repo.recent_user_events(...)`.

**Steps:**
- [ ] Failing tests: `append_example` produces a block byte-compatible with `formulate_criteria`'s grammar (round-trip: a criteria built by formulate_criteria + append_example parses under cap_examples with n+1 blocks); cap drops oldest first and preserves description; attach appends `<positive>` (assert criteria contains the thread's subject) and detach appends `<nearmiss>`; `add_example=false` leaves criteria untouched; extraction prompt contains the corrections section when events exist and omits it when empty; engine threads the exemplars (canned-LLM test asserting the built user message).
- [ ] Implement → pass; full suite green.
- [ ] Commit: `feat(task-engine): corrections feed criteria examples + extraction exemplars (spec §4.6)`

---

### Task 3: Client types + SSE union

**Files:** Modify `client/src/lib/api.ts`, `client/src/lib/sse.ts`.

**Interfaces produced (exact — every later task consumes these):**

```typescript
// api.ts
export type TaskSummary = { entities: number; pending_reviews: number; last_event_at: string | null }
export type Task = { id: string; name: string; goal: string; kind: string; status: 'active' | 'paused'; version: number; summary: TaskSummary }
export type TaskStateSchema = {
  version: number
  entity: { noun: string; identity_hint: string; attributes: { key: string; type: string; values?: string[] | null }[] } | null
  pipeline: { stages: string[]; terminal: string[] }
}
export type TaskDetail = Task & { state_schema: TaskStateSchema | null }
export type TaskEntity = { id: string; entity_key: string; display_name: string; state: Record<string, string | null>; updated_at: string }
export type TaskEvent = {
  id: string; field: string | null; old_value: string | null; new_value: string | null
  evidence_quote: string | null; confidence: number | null; origin: 'llm' | 'user'
  status: 'applied' | 'pending_review' | 'rejected' | 'reverted'
  pending_reason: string | null; proposed_entity: string | null
  thread_id: string | null; message_id: string | null; gmail_message_id: string | null
  entity_id: string | null; created_at: string
}
export type TaskDraftProposal = { name: string; description: string; state_schema: TaskStateSchema; keyword_probes: string[] }
export type TaskDraftPoll =
  | { status: 'pending' } | { status: 'gone' }
  | { status: 'ready'; proposal: TaskDraftProposal; positives: PreviewExample[]; near_misses: PreviewExample[] }

// fetchers (getJSON/fetch patterns of the file):
postTaskDraft(goal) → {draft_id}            // POST /api/tasks/draft, 202
getTaskDraft(draftId) → TaskDraftPoll        // 200/202/404 mapping like getBucketDraftPreview
createTask(body: { name; goal; description; state_schema; keyword_probes; confirmed_positives; confirmed_negatives }) → Task  // 201
getTasks() → { tasks: Task[] }
getTask(id) → TaskDetail
patchTask(id, body: { name?; status?; state_schema? }) → TaskDetail
deleteTask(id) → void                        // 204
getTaskBoard(id) → { entities: TaskEntity[] }
getTaskEvents(id, opts?: { status?; entity_id?; page?; limit? }) → { events: TaskEvent[] }
getTaskThreads(id) → { threads: InboxThread[] }
attachThread(id, threadId, addExample: boolean) → void       // POST, 201
detachThread(id, threadId, addExample: boolean) → void       // DELETE + ?add_example=
approveEvent(id, eventId) / rejectEvent(id, eventId) / revertEvent(id, eventId) → void
setEntityState(id, entityId, field, value) → void
mergeEntity(id, entityId, intoEntityId) → void
```

```typescript
// sse.ts SseDataEvent union additions
| { event: 'task_draft_ready'; draft_id: string }
| { event: 'task_updated'; task_id: string; version: number; pending_count: number }
| { event: 'task_backfill_progress'; task_id: string; scanned: number; matched: number; done: boolean }
```

(Verify each fetcher's path/method/status against `server/app/api/tasks.py` — the route table there is the source of truth; e.g. check whether events responses are wrapped in `{"events": ...}` or bare lists and match the actual shape.)

**Steps:**
- [ ] Implement; `cd client && bun run build 2>&1 | tail -3` clean.
- [ ] Commit: `feat(client): task API types/fetchers + task SSE events`

---

### Task 4: TasksProvider

**Files:** Create `client/src/state/TasksProvider.tsx`; Modify `client/src/AppShell.tsx` (mount inside InboxProvider).

**Interfaces produced:**

```typescript
export function useTasksStore(): {
  tasks: Task[]                       // list, refreshed on mount + SSE
  byId: Record<string, Task>
  refresh: () => Promise<void>        // list refetch
  getDetail: (id: string) => TaskDetail | undefined   // cached
  loadDetail: (id: string) => Promise<TaskDetail | null>  // fetch + cache; null on 404 (also evicts + list refetch)
  backfill: Record<string, { scanned: number; matched: number; done: boolean }>
  createTask: (...) => Promise<Task>  // wraps api.createTask + refresh
  patchTask / deleteTask: (...)       // wrap + refresh (+ evict detail)
}
```

SSE routing (one `subscribeSse` in the provider):
- `task_updated {task_id, version, pending_count}`: if `task_id` unknown → `refresh()` (create from elsewhere). Else refetch the task's detail+list item when `version > known.version || pending_count !== known.summary.pending_reviews` (the 2A no-bump rule makes the pending_count clause load-bearing — comment it). Refetch 404 → evict + `refresh()`.
- `task_backfill_progress` → update `backfill[task_id]`; on `done` also `loadDetail(task_id)`.
- `_open` → `refresh()` (reconnect catch-up), initial `refresh()` on mount.

**Steps:**
- [ ] Implement; mount `<TasksProvider>` inside `<InboxProvider>` in AppShell (comment: hook order — InboxProvider outer, Tasks inner; both permanent). Build clean; self-review effect deps/cleanup (single subscription, stable callbacks).
- [ ] Commit: `feat(client): TasksProvider — task store + version/pending-gap SSE refetch`

---

### Task 5: NewTaskWizard + SchemaEditor

**Files:** Create `client/src/pages/task/NewTaskWizard.tsx`, `client/src/pages/task/SchemaEditor.tsx`.

Clone `client/src/pages/buckets/NewBucketModal.tsx`'s machine EXACTLY where it applies (read it first): steps `'form' | 'pending' | 'review' | 'creating'`; `appliedRef` per-draft idempotency; SSE fast path (`task_draft_ready` for this draft_id → `getTaskDraft` fetch → apply) + 5s poll fallback (`getTaskDraft`), stop on ready/gone/unmount; `Backdrop`/modal styles reused (extract them to a tiny shared module or copy — implementer's call, note it).

Wizard specifics beyond the clone:
- `form`: single `goal` textarea ("I'm job hunting — track every company I'm in process with…") + start button → `postTaskDraft(goal)`.
- `review`: editable `name` + `description` inputs (seeded from proposal); **SchemaEditor** over the proposed `state_schema`; candidate examples list (positives/near-misses with confirm/reject radios — reuse the ExampleRow pattern verbatim); "create task" → `createTask({name, goal, description, state_schema, keyword_probes: proposal.keyword_probes, confirmed_positives, confirmed_negatives})` → step `creating`.
- `creating`: backfill progress from `useTasksStore().backfill[task.id]` ("scanned N · matched M"); on `done` → navigate(`/tasks/${task.id}`) and close.
- 422 from create (schema invalid after edits) → render the error message inline on the review step, stay editable.

`SchemaEditor` props `{ value: TaskStateSchema; onChange: (s: TaskStateSchema) => void }`: pipeline stages + terminal as editable chip lists (add/remove/rename via inline input); entity section (noun + identity_hint inputs; attributes table: key + type select from the five types + comma-separated values input shown only for enum; add/remove rows); "singleton task" checkbox toggling `entity: null` (preserving the last entity object in local state so un-toggling restores it). Purely controlled; no validation beyond non-empty trims (server 422 is the validator).

**Steps:**
- [ ] Implement; build clean; self-review the appliedRef/poll lifecycle against the NewBucketModal original (same cancellation semantics) and the 422 path.
- [ ] Commit: `feat(client): task creation wizard with schema editor + live backfill`

---

### Task 6: /tasks/:taskId route + TaskDetail shell

**Files:** Modify `client/src/App.tsx` (route `<Route path="/tasks/:taskId" element={<TaskDetail />} />`); Create `client/src/pages/task/TaskDetail.tsx`.

TaskDetail: `useParams()` → `loadDetail(taskId)` on mount (null → "task not found" + link home); header row (task name; status chip with pause/resume button → `patchTask`; delete button with `confirm()` → `deleteTask` → navigate('/')); three-region layout: PipelineBoard (main), ReviewFeed (right column or below, ~360px), ThreadsPanel (collapsible section below). Board/events/threads data owned HERE (not the provider): `getTaskBoard`/`getTaskEvents`/`getTaskThreads` on mount + refetch all three whenever the provider's cached detail for this id changes identity (the provider already refetches detail on SSE gaps — effect keyed on `getDetail(taskId)?.version` + `.summary.pending_reviews`). Sort threads client-side by `recent_message.internal_date` desc (API is unordered).

**Steps:**
- [ ] Implement with placeholder children (`<div>board</div>` etc. if Task 7/8 not yet merged — keep components in separate files so 7/8 replace placeholders); build clean.
- [ ] Commit: `feat(client): /tasks/:taskId route + detail shell with SSE-driven refetch`

---

### Task 7: PipelineBoard + EntityDrawer

**Files:** Create `client/src/pages/task/PipelineBoard.tsx`, `client/src/pages/task/EntityDrawer.tsx`.

`PipelineBoard({ schema, entities, onMove, onOpenEntity })`: columns = `[...schema.pipeline.stages, ...schema.pipeline.terminal]` (terminal visually dimmed); singleton schema (`entity: null`) renders the single `_self` entity as a one-row board. Cards grouped by `entity.state.stage ?? '(no stage)'` bucket (unstaged column first when non-empty); card shows `display_name` + up to 3 non-stage state fields; "move to ▾" `<select>` on each card → `onMove(entityId, stage)` (TaskDetail wires to `setEntityState(taskId, entityId, 'stage', stage)` then refetches board+events). No drag library — the select IS v1's correction gesture.

`EntityDrawer({ taskId, entity, schema, events, onClose, onEdit, onMerge, onRevert })`: opened by card click; shows all state fields with inline edit (text input per attribute; enum → select of `values`; commit on blur → `onEdit(field, value)`; 422 → inline error); entity's event history (filtered `entity_id`, newest first) with evidence quotes + revert buttons on applied events; "merge into…" select of other entities → `confirm()` → `onMerge(intoId)`.

**Steps:**
- [ ] Implement; build clean; self-review: stage select must not fire onMove for the current stage; drawer edits round-trip through TaskDetail's refetch.
- [ ] Commit: `feat(client): pipeline board + entity drawer with inline corrections`

---

### Task 8: ReviewFeed + ThreadsPanel

**Files:** Create `client/src/pages/task/ReviewFeed.tsx`, `client/src/pages/task/ThreadsPanel.tsx`.

`ReviewFeed({ events, entitiesById, onApprove, onReject, onRevert })`: top section "Needs review (N)" — `pending_review` events as cards: entity name (via entity_id, falling back to `proposed_entity` with a "new entity?" hint), `field: old → new`, evidence blockquote, confidence, **human-readable pending_reason** (map the five reasons to copy, e.g. `near_duplicate_entity` → `"LLM proposed '{proposed_entity}' — close to an existing entity"`, `fence_blocked` → `"an older email tried to change something you corrected"`), Approve/Reject buttons. Below: "Recent activity" — newest 30 non-pending events, origin badge (LLM/you), status, revert button on applied. All actions → callbacks (TaskDetail wires api calls + refetch).

`ThreadsPanel({ taskId, threads, bucketsById, onDetach, onAttach })`: attached list rendered via `InboxList`-style rows (reuse `InboxList` with an extra actions column is invasive — build a thin local row component reusing its abbreviate helper by export or copy; note the choice) with a detach button + "teach the task" checkbox (maps to `add_example`, default checked, tooltip: "adds this as a near-miss example so the task stops matching threads like it"); "Add emails" section: `SearchBar` + `useInboxSearch` reuse, results rows with an attach button (+ same checkbox semantics for positive examples).

**Steps:**
- [ ] Implement; build clean.
- [ ] Commit: `feat(client): review tray with pending reasons + threads panel with search-to-attach`

---

### Task 9: HUD task cards

**Files:** Modify `client/src/pages/hud/HudPage.tsx`.

Above the buckets section: "Tasks" grid from `useTasksStore().tasks` — card per task: name, status chip (paused dimmed), `summary.entities` entities, pending badge (`summary.pending_reviews > 0` → amber "N to review"), `last_event_at` ago-label (reuse `agoLabel`), backfill progress line when `backfill[id]` exists and not done; click → navigate(`/tasks/${id}`). "+ New task" card/button → renders `NewTaskWizard` (modal state local to HudPage). Empty state: "No tasks yet — create one from a goal."

**Steps:**
- [ ] Implement; build clean.
- [ ] Commit: `feat(client): HUD task cards + new-task entry`

---

### Task 10: Reference docs

**Files:** Modify `reference/TASKS_INDEX.md` (learning loop, pending reasons, detach-rejects-pendings, new API body params), `reference/CLIENT_INDEX.md` (TasksProvider + routing table + task pages + wizard), `reference/MANIFEST.md` (rows + own top stamp — fix the stale top-stamp nit from 2A while there).

- [ ] Verify every claim against source; stamp all touched docs + rows to the final code commit; commit: `docs(reference): re-index tasks/client for phase 2b`
- [ ] Final: server suite green; client build clean.

---

### Task 11 (manual — coordinator + user): Worked-example acceptance

The spec's Phase 2 ship gate. `scripts/dev.sh`, real Gmail account:
1. Create a tracker from a real goal via the wizard; edit a stage name in the schema editor; confirm/reject candidates; watch backfill populate the board chronologically.
2. Verify a real incoming email moves an entity (≤35s poll → triage → extract → board updates live via SSE).
3. Both corrections: detach a wrongly-attached thread (entity reverts; criteria gains a near-miss — verify via `GET /api/tasks/{id}` criteria growth or DB peek); drag ("move to") an entity backward (fence: verify a stale email can't re-promote it — check the pending event carries `fence_blocked`).
4. Review tray: approve one pending, reject one; verify board + counts update everywhere (HUD card badge included).
5. `llm_calls` sanity: `SELECT stage, count(*), sum(cost_usd) FROM llm_calls GROUP BY stage` — confirm triage/extract/propose all recorded with task_id where expected.
