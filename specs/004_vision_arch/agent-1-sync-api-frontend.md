<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 1 — incremental evolution -->

# Sync, API Surface, and Frontend Inversion

---

## 1. Backend ↔ Gmail: from 30s poll to push (Phase 2)

**Principle: push is a new *trigger* for the existing sync engine, not a new
engine.** `poll_new_messages` in `server/app/workers/tasks.py` — with its
`sync_lock`, `HistoryGoneError` fallback, cursor semantics, and noise filter —
stays the single entry point for "bring this user current". We only make it
fire within ~1s of mail arriving instead of within 30s.

### Gmail `users.watch` → Cloud Pub/Sub → webhook

- **GCP setup (one-time, same project as the OAuth client):** Pub/Sub topic
  `projects/<proj>/topics/gmail-push`; grant
  `gmail-api-push@system.gserviceaccount.com` the Publisher role; one **push
  subscription** targeting `https://<railway-api-domain>/api/gmail/push` with
  OIDC token auth (service account + audience).
- **Watch lifecycle:** new module `server/app/gmail/watch.py` —
  `start_watch(gmail, *, topic) -> (history_id, expiration)` (calls
  `users().watch(labelIds=["INBOX"], labelFilterBehavior="INCLUDE")`),
  `stop_watch(gmail)`. Stored on `User` (`gmail_watch_expiration`,
  `gmail_watch_topic`, migration 0007).
- **Establishment:** on first SSE connect (`api/sse.py` kickoff branch) and at
  OAuth callback, enqueue Celery `ensure_gmail_watch(user_id)` — idempotent:
  re-watch if expiration < 48h away or null.
- **Renewal:** second beat entry in `workers/beat_schedule.py`:
  `renew-gmail-watches-daily` → `renew_gmail_watches` (iterate users with
  non-null refresh token, `ensure_gmail_watch` each, jittered `countdown`
  spread over 10 min to avoid a thundering herd; skip + null-out users whose
  refresh token is revoked).
- **Webhook:** `POST /api/gmail/push` in a new `server/app/api/gmail_push.py`.
  No session cookie — auth is the Pub/Sub OIDC bearer token, verified with
  `google.auth.jwt` against the configured audience (`settings.gmail_push_audience`,
  new env `GMAIL_PUSH_AUDIENCE` + `GMAIL_PUSH_TOPIC` in `app/config.py` /
  `.env.example`). Body decodes to `{emailAddress, historyId}` → look up user
  by email → **debounce**: `SET push_debounce:{uid} 1 NX EX 3` in redis (same
  pattern as `realtime/sync_lock.py`; new `realtime/push_debounce.py`) — Gmail
  sends bursts of notifications per change; one poll per 3s window is enough →
  `poll_new_messages.apply_async([uid])`. Always return 204 fast (Pub/Sub
  retries non-2xx; a failed lookup is just logged).
- **Crucially:** the notification's `historyId` is *not* trusted as a cursor —
  `poll_new_messages` keeps using `users.gmail_last_history_id`. Push is purely
  a doorbell. This is what keeps the change small and safe.

### Beat demoted to reconciliation

`enqueue-polls-every-30s` stays (push can drop; watch can lapse) but the
fan-out gets cheaper: `enqueue_polls` skips users whose watch is healthy
(`gmail_watch_expiration > now`) unless 120s have passed since their last poll
(tracked in a redis key `last_poll:{uid}` SET EX by `poll_new_messages`).
Active-user gating via the `active_users` zset is **unchanged** — push events
for inactive users still sync (cheap, keeps task state current even when no tab
is open), which is exactly what the task engine wants. This supersedes the
"inactive-user hourly poll" open question in
`specs/002_inbox_sync/05_18_26-open-questions.md` §1.4: push covers inactive
users; the watch-renewal job is the only per-user periodic cost.

### Label/delete mirroring (same phase)

`gmail_sync.fetch_history_records` widens `historyTypes` from
`["messageAdded"]` to `["messageAdded", "labelAdded", "labelRemoved",
"messageDeleted"]` (still `labelId="INBOX"` scoped). `partial_sync_inbox`
handles the new record shapes:

- `labelRemoved` containing `INBOX` → set `InboxThread.is_archived = True`
  (column from migration 0007); `labelAdded INBOX` → un-archive.
- `messageDeleted` → delete the `inbox_messages` row, recompute
  `recent_message_id` (existing query in `inbox_repo.upsert_message` is
  factored out to `inbox_repo.recompute_recent_message`), delete the thread if
  empty.
- `full_sync_inbox` already wipes+repopulates, so it's consistent by
  construction; it additionally records `is_archived=False` for everything it
  ingests (it only lists INBOX).

`GET /api/inbox` gains `?include_archived=` (default false). The
`threads_updated` SSE payload is unchanged — archived/deleted threads simply
come back different (or absent) from `POST /api/threads/batch`, and
`useInbox.applyThreadUpdates` drops ids the batch didn't return (small client
change: today missing ids are ignored; they must now be evicted from
`idLayer`/`displayLayer`).

**Acceptance criterion (answers 002 §3.3):** an email arriving in Gmail is
visible in an open tab in ≤5s p50 (vs ≤35s today); an archive in Gmail
disappears from the inbox view in ≤5s p50.

---

## 2. Frontend ↔ backend: SSE stays

Decision for 002 §2.1: **keep SSE**, fix the "websocket" terminology in older
specs. Rationale: the entire flow is server→client; the one client→server
"live" need (corrections) is plain POSTs. `realtime/pubsub.py`'s
`PubSubDispatcher`, `sse_connections.py`, and `lib/sse.ts` are reused untouched.

`SseDataEvent` union in `client/src/lib/sse.ts` grows:

| Event | Payload | Producer | Consumer |
|---|---|---|---|
| `threads_updated` | `{thread_ids}` | sync tasks (existing) | `useInboxSse` (existing) |
| `extend_complete` | `{thread_ids, more}` | existing | existing |
| `task_draft_ready` | `{draft_id, name, criteria_description, state_schema, positives, near_misses}` | `propose_task_draft` | task wizard (poll fallback: `GET /api/tasks/draft/{id}`, same dual-resolution as `NewBucketModal`) |
| `task_updated` | `{task_id, entity_ids, event_ids, pending_count}` | `process_task_updates`, correction endpoints, `execute_task_action` | `useTaskBoard`, HUD task cards |
| `task_backfill_progress` | `{task_id, scanned, matched, done}` | `backfill_task` | wizard / task page progress bar |

Consumers re-read via the API on event receipt (the `threads_updated` →
`/api/threads/batch` pattern) — events carry ids, not rows, preserving the
publish-after-commit replay guarantee. Burst coalescing (002 §2.8):
`process_task_updates` publishes once per task per run, not per event;
`backfill_task` publishes every 50 threads.

---

## 3. API surface (FastAPI)

New router `server/app/api/tasks.py` (`prefix="/api"`, registered in
`app/main.py` next to the existing routers), plus `api/gmail_push.py` and
`api/search.py`. Auth: everything except `/api/gmail/push` uses
`deps.get_current_user`. Ownership checks mirror `_load_owned_or_403` in
`api/buckets.py`.

```
# Phase 0
GET    /api/search?q=&page=&limit=&task_id=&include_archived=   → ranked thread list (search_repo)

# Phase 2
POST   /api/gmail/push                                          → Pub/Sub OIDC-verified doorbell (204)

# Phase 3 — tasks CRUD + draft
GET    /api/tasks                          → {tasks:[{id,name,goal,kind,status,action_mode,
                                              summary:{entities_by_stage, pending_reviews, last_event_at}}]}
POST   /api/tasks/draft {goal}             → 202 {draft_id}        (mirrors /buckets/draft/preview)
GET    /api/tasks/draft/{draft_id}         → 200 ready | 202 pending | 404   (poll fallback)
POST   /api/tasks {name, goal, criteria_description, confirmed_positives,
                   confirmed_negatives, state_schema}            → 201 task; enqueues backfill_task
GET    /api/tasks/{id}                     → task detail incl. state_schema
PATCH  /api/tasks/{id} {name?|status?|state_schema?|action_mode?}
DELETE /api/tasks/{id}                     → soft delete (is_deleted)

# Phase 3 — board / events / corrections
GET    /api/tasks/{id}/board               → {entities:[{id, entity_key, display_name, state, updated_at}]}
GET    /api/tasks/{id}/events?status=&entity_id=&page=           → event feed (newest first, evidence quotes)
GET    /api/tasks/{id}/threads?page=       → linked threads (state='attached')
POST   /api/tasks/{id}/threads {thread_id, add_example: bool}    → user attach (+ extraction kick)
DELETE /api/tasks/{id}/threads/{thread_id}?add_example=          → user detach (+ revert sourced events)
POST   /api/tasks/{id}/events/{event_id}/approve|reject|revert   → review-tray + undo actions
POST   /api/tasks/{id}/entities/{entity_id}/state {field, value} → manual state edit (user-origin event)
POST   /api/tasks/{id}/entities/{entity_id}/merge {into_entity_id}

# Phase 5 — actions
GET    /api/tasks/{id}/actions?status=
POST   /api/tasks/{id}/actions/{action_id}/approve|reject
PUT    /api/tasks/{id}/grants {action_type, granted: bool}       → consent toggles for auto mode
GET    /auth/login?upgrade=actions                               → incremental OAuth scope upgrade

# Transitional (Phase 4, one release)
/api/buckets*  → shim over tasks kind='classify'; then removed
```

Inbox routes (`api/inbox.py`) change only by `?include_archived=`. SSE route
unchanged. `GET /api/health` unchanged.

---

## 4. Frontend: inbox-first → HUD-first

The SPA currently has **no router** — `App.tsx` switches on auth state and
renders `Home.tsx`, which is the inbox. Inversion plan:

### Phase 1 — routing shell (no feature change)

- `bun add react-router-dom` (v7) in `client/`.
- `App.tsx`: authed branch renders `<BrowserRouter>` with routes `/` →
  `HudPage`, `/inbox` → `InboxPage`, `/tasks/:taskId` → `TaskPage` (Phase 3),
  `*` → redirect `/`. The FastAPI SPA catch-all in `app/main.py` already
  serves `index.html` for unknown paths, so deep links work with zero backend
  change.
- `pages/Home.tsx` is renamed `pages/inbox/InboxPage.tsx` content-intact (it
  already composes `useBuckets` + `useInbox` + `useInboxSse` +
  `SecondaryHeader` + `InboxList` + modals). A thin `AppShell` component
  (top nav: HUD / Inbox links, auth header from `useAuth`) wraps both pages.
- `HudPage` v1 (`client/src/pages/hud/HudPage.tsx`): global search bar (hits
  `GET /api/search`, results reuse `InboxList` row rendering), a "recently
  processed" strip — last N threads by sync recency, satisfying the
  "see how up-to-date the system is" requirement from
  `specs/003_task_hud/flows.md` — and bucket summary cards (counts per bucket
  from the already-loaded inbox snapshot). The SSE singleton keeps one
  EventSource per tab regardless of route (subscription moves up to `AppShell`
  level so navigating doesn't drop the `active_users` registration).

### Phase 3 — task surfaces

New directory `client/src/pages/tasks/`:

- `useTasks.tsx` — list + CRUD, modeled line-for-line on
  `pages/buckets/useBuckets.tsx` (fetch on mount, mutate-then-refresh, `byId`).
- `NewTaskWizard.tsx` — modeled on `NewBucketModal.tsx`'s
  `form|pending|review` steps + `appliedRef` idempotency + SSE/poll dual
  resolution, with the added schema-editor panel in review (pipeline chips,
  field add/remove from the five kinds).
- `TaskPage.tsx` (`/tasks/:taskId`) — three regions:
  **Board** (`useTaskBoard`: `GET /api/tasks/{id}/board`, re-fetch on
  `task_updated`; pipeline fields render as columns with entity chips,
  drag-between-columns = manual state edit POST), **Event feed**
  (`useTaskEvents`, paged; each card: evidence quote, old→new, origin badge,
  approve/reject/revert buttons; provenance click → thread peek), **Threads
  tab** (linked threads list with detach; "Add emails" opens a search-backed
  picker → attach endpoint — this is the "pull down emails / manually indicate"
  flow from `specs/003_task_hud/flows.md`).
- `HudPage` v2: task cards become the primary grid — per-task stage histogram,
  `pending_reviews` badge (deep-links to the review tray), `last_event_at`.
  Inbox demoted to a nav link + the recency strip. A global **review tray**
  drawer aggregates `pending_review` events across tasks.
- Inbox rows gain an overflow action "Add to task…" (task picker → attach
  endpoint) so correction is reachable from every surface that shows a thread.

### Phase 4/5 — unification + actions UI

- Bucket modals (`NewBucketModal`, `ViewBucketsModal`) deleted; classify-kind
  tasks are created through the wizard with the schema step skipped;
  `FilterByBucketDropdown` reads `useTasks` (kind='classify') instead of
  `useBuckets`.
- Task settings panel: action_mode dial, per-action-type grant toggles, scope
  upgrade CTA when `gmail_granted_scopes` lacks `gmail.modify`; proposed
  actions appear in the review tray with one-click approve and an undo toast
  for auto-executed ones.

### What is deliberately NOT rebuilt

`useInbox`'s `idLayer`/`displayLayer` LWW model, auto-extend, watchdogs,
`useInboxSse`'s snapshot/buffer lifecycle, `lib/api.ts`, `lib/sse.ts` reconnect
— all survive unchanged. The inbox page is the proof-of-sync view the vision
explicitly keeps; only the bucket-creation watchdogs in `Home.tsx`
(60s/150s `resync()` timers) disappear in Phase 4 with `reclassify_user_inbox`
itself, replaced by `task_backfill_progress` events.

---

## 5. Testing & ops notes

- All new Celery paths follow the existing eager-mode contract
  (`CELERY_TASK_ALWAYS_EAGER=1`, `SessionLocal` module-attr monkeypatch seam in
  `task_engine_tasks.py`, fakeredis for debounce/locks) so `server/tests`
  patterns carry over.
- The webhook gets a unit test with a forged-OIDC rejection case; watch renewal
  gets a clock-skew test (expiration < 48h triggers re-watch).
- New env vars (added to `.env.example`, never read from `.env`):
  `GMAIL_PUSH_TOPIC`, `GMAIL_PUSH_AUDIENCE`.
- Observability (answers 002 §3.2): structured log counters at the existing
  `log.info` choke points — `_publish` already logs subscriber counts; add
  push→poll latency (webhook receipt → publish), extraction
  applied/pending/rejected counts per run, and watch-renewal failures.
- Railway: no new services. The webhook rides the existing API service;
  Pub/Sub is GCP-side config. Beat remains single-replica
  (`railway.beat.toml`), now carrying two schedule entries.
