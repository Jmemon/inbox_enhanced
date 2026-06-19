<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 2 — data-layer first -->

# API Surface & HUD-First Frontend

> The React app inverts: today `Home.tsx` *is* the inbox; in the vision the
> HUD is home and the inbox is a view you can navigate to. This file names the
> route/page-level changes; component internals are implementation detail.

## 1. API surface (FastAPI routers, by phase)

### Existing routes (kept, behavior deltas only)

| Route | Delta |
|-------|-------|
| `GET /api/inbox` (`server/app/api/inbox.py`) | gains `?include_archived=&q=` passthrough; sort moves to `inbox_threads.last_activity_at` (no join); serializer adds `processing_state`, `is_archived`, `is_unread`, task chips (`task_links` summary, Phase 4) |
| `POST /api/threads/batch`, `GET /api/threads/{id}` | unchanged contract; serializer additions as above; thread detail gains full messages w/ `body_text` (the inbox becomes clickable — needed for evidence display) |
| `POST /api/inbox/refresh` / `extend` | unchanged (refresh becomes rarely needed once push lands) |
| `/api/buckets*` (`server/app/api/buckets.py`) | unchanged through Phase 4; Phase 5: alias over tasks then removed (see `agent-2-04` §6) |
| `GET /api/sse` (`server/app/api/sse.py`) | `id:` frames + `Last-Event-ID` replay (see `agent-2-03` §3) |

### New routes

**Phase 1 — search** (`server/app/api/search.py`):

- `GET /api/search?q=&task_id=&bucket_id=&from=&after=&before=&include_archived=&page=`
  → `{hits: [{thread, match_snippet, rank}], total, page}` — backed by
  `inbox/search_repo.search_threads`.

**Phase 2 — sync** (`server/app/api/gmail.py`):

- `POST /api/gmail/webhook` — Pub/Sub push (OIDC-verified, not
  session-auth'd; excluded from `deps.get_current_user`).
- `GET /api/sync/status` — `{last_history_id_age_s, watch_expiration,
  gmail_sync_status, last_sync_at}` — feeds the HUD "how up-to-date am I"
  header and is the observability surface OQ 3.2 wanted.

**Phase 4 — tasks** (`server/app/api/tasks.py`):

- `POST /api/tasks/draft` → 202 `{draft_id}`; result via SSE + pollable
  `GET /api/tasks/draft/{draft_id}` (the `preview_cache` pattern, verbatim).
- `GET /api/tasks` (`?kind=`), `POST /api/tasks`, `GET /api/tasks/{id}`
  (task + entities + counts), `PATCH /api/tasks/{id}` (name/goal/criteria/
  schema/status), `DELETE` (soft).
- `GET /api/tasks/{id}/entities`; `GET /api/tasks/{id}/events?status=&entity_id=`
  (timeline + review queue feed).
- `POST /api/tasks/{id}/links {thread_id}` / `DELETE /api/tasks/{id}/links/{thread_id}`.
- `POST /api/tasks/{id}/events` (manual event);
  `POST /api/tasks/{id}/events/{event_id}/resolve {action: approve|reject}`;
  `POST /api/tasks/{id}/events/{event_id}/revert`.
- `POST /api/tasks/{id}/re_enrich` → 202 (manual "re-run this task").

**Phase 6 — actions:**

- `PATCH /api/tasks/{id}/policy {action_kind: level}` (403 +
  `scope_upgrade_required` until granted).
- `GET /auth/upgrade?scopes=actions` — incremental OAuth re-consent.
- `POST /api/threads/{id}/modify {op}` — user-initiated archive/read
  write-through.

All task mutations publish through `realtime/event_log.append` after commit.

## 2. Frontend (client/src) — HUD inversion

Routing today is a conditional render in `App.tsx`
(`loading|anon|authed → Splash|Login|Home`). Add **react-router** (bun add;
the only new client dep this plan needs) with authed routes:

| Route | Page | Content |
|-------|------|---------|
| `/` | `pages/hud/Hud.tsx` (new home) | Task cards grid (name, entity-stage summary, review badge, last activity); **global search bar** (→ `/search`); **freshness strip**: last ~10 processed emails + `sync_status`/`GET /api/sync/status` indicator (the 003 flows "see how up-to-date it is" requirement); "new task" entry |
| `/task/:id` | `pages/task/TaskBoard.tsx` | Entity pipeline board (one column per `schema_json.stages`); entity card → evidence timeline (`task_events` + linked threads w/ snippets); **review queue panel** (proposed events + low-confidence links, approve/reject); attach-thread affordance (inline search using `/api/search?task_id=exclude`) |
| `/task/new` | `pages/task/NewTaskWizard.tsx` | goal → proposed schema/criteria/candidates → confirm loop (mirrors `NewBucketModal.tsx`'s form/pending/review steps, including the SSE+poll dual delivery) |
| `/inbox` | existing `Home.tsx` content, relocated `pages/inbox/InboxPage.tsx` | current list + pagination + bucket filter, plus search box, task chips per row, attach-to-task row action, "N new" pill |
| `/search` | `pages/search/SearchPage.tsx` | results with `match_snippet` highlights, filter rail (task/bucket/sender/date), same row affordances as inbox |

Hooks/lib changes:

- `lib/sse.ts`: `SseDataEvent` union grows (`thread_upserted`,
  `thread_enriched`, `task_state_changed`, `task_review_pending`,
  `sync_status`, `resync_required`); singleton unchanged.
- New `pages/hud/useTasks.tsx` (CRUD + SSE-driven refresh), `useTaskBoard.tsx`
  (entities/events for one task, applies `task_state_changed` incrementally),
  `useSearch.tsx` (debounced).
- `useInbox.tsx` / `useInboxSse.tsx` survive for `/inbox`; watchdog timers
  removed per `agent-2-03` §4; `Home.tsx`'s reclassify timers deleted.
- `lib/api.ts` grows the corresponding fetchers; 401 handling unchanged.

The inbox page is intentionally **not** redesigned — it is demoted, gaining
only the affordances the task loop needs (chips, attach, search). HUD design
effort goes to `/` and `/task/:id`, where the product now lives.

## 3. Serving / build

No change to the model: Vite builds to `server/app/static/`
(`scripts/build_frontend.sh`), FastAPI SPA catch-all in
`server/app/main.py` already serves any non-`api/auth/assets` path, so the
new client-side routes work without server changes. `vite.config.ts` proxy
untouched.
