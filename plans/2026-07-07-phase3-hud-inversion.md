# Phase 3 — HUD Inversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Spec: `specs/004_vision_arch/chosen-architecture.md` §3 Phase 3.
> Plan stamped at commit `656db34` on branch `main`.

**Goal:** The HUD becomes the product surface: task cards gain stage histograms, an aggregated cross-task review tray and an activity ticker land on `/`, the page reorders task-first, and a second beat entry polls tracker owners hourly so tasks advance while nobody is watching.

**Architecture:** Small server surface (one beat entry + task, `stage_counts` folded into the existing summary serializer at zero extra queries, two read-only aggregate routes with task/entity joins) + HUD-only client work reusing Phase 2B's components and conventions (TasksProvider convergence, ReviewFeed's busy pattern, `pendingReasonCopy` promoted to a shared module). Much of spec-Phase-3 already shipped in 2B (cards, badges, nav demotion) — this plan is the delta.

**Tech Stack:** existing (uv / bun; no new dependencies).

## Global Constraints

- All Phase 0–2 conventions hold: `uv`/`bun` only; NEVER read `.env`; repo functions never commit; read helpers never flush; publish-after-commit; TDD server-side (suite baseline **420 passed** + whatever the 2C hardening branches add — rebase the integration branch on main AFTER 2C merges and re-baseline); client verification = build + tsc + reviewer probes; worktrees lack `.env` → settings assertions via resolved `get_settings()`.
- Beat stays SINGLE REPLICA (`railway.beat.toml`); the new entry must be cheap when nothing qualifies.
- Deterministic hashing for the poll spread: `zlib.crc32(uid.encode()) % 3600` — Python's builtin `hash()` is process-salted and MUST NOT be used.
- Aggregate routes are user-scoped joins (a cross-user leak here exposes other people's email-derived text — treat scoping as a security boundary and probe it in review).
- Commit per task, `type(scope): summary`, no attribution lines.

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `server/app/workers/beat_schedule.py` + `workers/tasks.py` | modify | hourly tracker-owner poll fan-out |
| `server/app/api/tasks.py` | modify | `stage_counts` in summary; `GET /api/reviews`; `GET /api/activity` |
| `server/app/task_engine/repo.py` | modify | `list_pending_events_for_user`, `list_recent_events_for_user` |
| `client/src/lib/api.ts` | modify | `stage_counts` on TaskSummary; feed types/fetchers |
| `client/src/pages/task/pendingReasons.ts` | create | shared reason→copy map (moved from ReviewFeed) |
| `client/src/pages/hud/ReviewTray.tsx`, `ActivityTicker.tsx` | create | aggregated panels |
| `client/src/pages/hud/HudPage.tsx` | modify | histogram line on cards; layout reorder |
| `reference/*` | modify (last) | re-index + stamps |

---

### Task 1: Hourly tracker-owner beat poll (server)

**Files:** Modify `server/app/workers/beat_schedule.py`, `server/app/workers/tasks.py`; Test extend `server/tests/test_tasks.py`.

- New beat entry `poll-tracker-owners-hourly` → `app.workers.tasks.enqueue_tracker_owner_polls`, `schedule(run_every=3600.0)` (comment: beat is single-replica; this covers users with active trackers who have no open tab — the 30s `enqueue_polls` only covers `active_users`).
- New task `enqueue_tracker_owner_polls()`: `SELECT DISTINCT user_id FROM tasks WHERE kind='tracker' AND status='active' AND is_deleted=false AND user_id IS NOT NULL AND state_schema IS NOT NULL`; exclude uids currently in `active_users.list_active()` (they're on the 30s path); for each remaining uid: `poll_new_messages.apply_async(args=[uid], countdown=zlib.crc32(uid.encode()) % 3600)` — sharded over the hour, deterministic.
- [ ] TDD: owner-not-active enqueued with crc32-derived countdown; active owner skipped; paused/deleted/schema-less tasks don't qualify; no trackers → no enqueues. Full suite green.
- [ ] Commit: `feat(workers): hourly tracker-owner poll so tasks advance offline`

---

### Task 2: `stage_counts` in the task summary (server)

**Files:** Modify `server/app/api/tasks.py` (`_serialize_summary` + its two callers pass the task row); Test extend `server/tests/test_tasks_api.py`.

- `_serialize_summary(db, *, task) -> dict` (signature change: takes the task row, derives `task.id` internally — update both call sites): adds `"stage_counts": {...}` computed from the SAME `list_entities` result already fetched (zero new queries): count `entity.state.get("stage")` values; key order = schema stage order (`validate_schema(task.state_schema).all_stages()` when schema present — wrap in try/except ValueError → observed order) with observed extras appended, `None`-staged entities under `"(no stage)"`. Classify-kind/schema-less tasks: observed order only.
- [ ] TDD: counts match seeded entities; ordering follows schema; no-stage bucket; empty task → `{}`. Full suite green.
- [ ] Commit: `feat(api): stage_counts in task summaries`

---

### Task 3: Aggregated feed routes (server)

**Files:** Modify `server/app/task_engine/repo.py`, `server/app/api/tasks.py`; Test extend `server/tests/test_tasks_api.py`, `test_task_repo.py`.

**Repo helpers** (read-only, never flush):

```python
def list_pending_events_for_user(db, *, user_id, limit=50) -> list[tuple[TaskEvent, Task]]
    # join tasks on task_id: task.user_id == user_id, not deleted; event status='pending_review';
    # newest first. Returns (event, task) pairs.
def list_recent_events_for_user(db, *, user_id, limit=20) -> list[tuple[TaskEvent, Task]]
    # same join; status != 'pending_review'; newest first.
```

**Routes** (both `Depends(get_current_user)`):
- `GET /api/reviews?limit=` (clamp 1..200, default 50) → `{"reviews": [ {..._serialize_event fields..., "task_id", "task_name", "entity_display_name"} ]}` — entity display resolved via one batched `IN` query over the events' entity_ids (no N+1), falling back to `proposed_entity` then null.
- `GET /api/activity?limit=` (clamp 1..100, default 20) → `{"activity": [ same shape ]}`.
- [ ] TDD: cross-user scoping (user B's pendings never appear — the security probe); deleted tasks excluded; ordering; entity display fallback chain; limits clamp. Full suite green.
- [ ] Commit: `feat(api): aggregated cross-task reviews + activity feeds`

---

### Task 4: Client types + card histograms

**Files:** Modify `client/src/lib/api.ts`, `client/src/pages/hud/HudPage.tsx`.

- `TaskSummary` gains `stage_counts: Record<string, number>`; new types `FeedItem = TaskEvent & { task_id: string; task_name: string; entity_display_name: string | null }`; fetchers `getReviews(limit?)` / `getActivity(limit?)` per the route envelopes.
- Task cards: under the entities line, a compact histogram line from `Object.entries(summary.stage_counts)` (insertion order = server order): `applied 4 · screen 2 · onsite 1` (omit when empty; truncate to first 4 stages + "+n more").
- [ ] Build + tsc clean. Commit: `feat(client): feed types + stage histograms on task cards`

---

### Task 5: Aggregated ReviewTray on the HUD

**Files:** Create `client/src/pages/task/pendingReasons.ts` (move the map out of ReviewFeed; ReviewFeed imports it — behavior identical), `client/src/pages/hud/ReviewTray.tsx`; Modify `client/src/pages/hud/HudPage.tsx`.

- `ReviewTray` (self-contained data ownership): fetches `getReviews()` on mount; card per item: task name (links to `/tasks/{task_id}`), entity display (fallback `proposed_entity` + "new entity?" then "unknown"), field old→new, evidence, human-readable reason (shared map), Approve/Reject via the existing `approveEvent`/`rejectEvent` (they take task_id + event_id) with ReviewFeed's per-item busy + 10s timeout pattern; on action success refetch the tray (TasksProvider convergence handles cards/badges via SSE). Refetch the tray when ANY task's `pending_reviews` changes — subscribe via `useTasksStore()` tasks reference changes (effect keyed on a memoized total-pending sum). Collapsed by default when N==0 ("Nothing needs review"), expanded header "Needs review (N)".
- [ ] Build + tsc clean; self-review busy-map cleanup + the total-pending effect (no loops). Commit: `feat(client): aggregated review tray on the HUD`

---

### Task 6: ActivityTicker + HUD layout reorder

**Files:** Create `client/src/pages/hud/ActivityTicker.tsx`; Modify `client/src/pages/hud/HudPage.tsx`.

- `ActivityTicker`: fetches `getActivity()` on mount + refetches on the same total-version signal (memoized sum of task versions from the store); renders compact lines: `{task_name}: {entity} {field} → {new_value} · {ago}` with origin badge; links to the task.
- HUD non-search layout order becomes: sync strip (top, unchanged) → **Tasks grid** → **Review tray** → **Activity** → Recently active (existing strip, renamed from "Recently processed") → Buckets (last). No other behavior changes.
- [ ] Build + tsc clean. Commit: `feat(client): activity ticker + task-first HUD layout`

---

### Task 7: Reference docs

- [ ] Update `TASKS_INDEX.md` (feed routes + repo helpers + stage_counts), `WORKERS_INDEX.md` + `INBOX_SYNC_INDEX.md` (second beat entry + tracker-owner fan-out; the "only active_users get polled" gotcha is now wrong — correct it), `CLIENT_INDEX.md` (ReviewTray/ActivityTicker/pendingReasons move/histograms), `MANIFEST.md`. Verify every claim; stamp to final code commit. Commit: `docs(reference): re-index for phase 3 hud inversion`
- [ ] Final: suite green; build clean.

---

### Task 8 (manual acceptance — coordinator + user)

Dev stack: HUD shows task-first layout with histograms; approve/reject from the tray converges card badges + the task page; activity ticker updates after a real email lands; with the tab CLOSED and beat running, confirm via logs (or `last_sync` timestamps) that a tracker owner still gets polled within the hour window.
