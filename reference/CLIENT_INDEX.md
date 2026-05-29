<!-- stamp: 13a07e5 (main) | 2026-05-29 -->

# Client Index

> Scope: React 19 + Vite browser SPA (client/src) — App/AuthProvider routing, lib/api fetch wrappers + lib/sse EventSource singleton, useInbox/useInboxSse/useBuckets hooks, inbox list + pagination + reload, bucket filter/new/view modals; the Browser↔API fetch+SSE realtime layer (§1.6, §2.1, §2.11, §3.4).

## Files
| Path | Role / key exports (components, hooks) |
|------|----------------------------------------|
| client/src/main.tsx | Entry. `ReactDOM.createRoot(#root)` renders `<App>` in `<React.StrictMode>`. |
| client/src/App.tsx | `App` (default) wraps `<AuthProvider>`; inner `Routes` switches on `useAuth().state.status`: `loading→<Splash>`, `anon→<Login>`, `authed→<Home>`. |
| client/src/auth/useAuth.tsx | `AuthProvider`, `useAuth()`, types `Me`/`AuthState`. Context state `loading|authed|anon`. `refresh()`→`getJSON('/auth/me')`, `signOut()`→`postEmpty('/auth/logout')`. `useEffect` calls `refresh()` on mount. |
| client/src/auth/Login.tsx | `Login` (default). Static card; button → `window.location.assign('/auth/login')`. Reads `?authError` from URL (`denied`→"sign-in cancelled"). |
| client/src/auth/Splash.tsx | `Splash` (default). Static "loading…" screen for the `loading` auth state. |
| client/src/lib/api.ts | Fetch wrappers + types. Exports `getJSON`/`postEmpty` (401→`throw {kind:'unauthorized'}`), `getInbox`, `getThread`, `getThreadsBatch`, `requestRefresh`, `getBuckets`, `createBucket`, `patchBucket`, `deleteBucket`, `postBucketDraftPreview`, `getBucketDraftPreview`, `postInboxExtend`. Types `InboxMessage`/`InboxThread`/`InboxPage`/`Bucket`/`BucketExampleIn`/`DraftPreviewPoll`/`AuthError`. All `credentials:'same-origin'`; verbose `console.log` timing. |
| client/src/lib/sse.ts | EventSource singleton. Exports `subscribeSse(handler)→unsub`, types `PreviewExample`/`SseDataEvent`/`SseConnEvent`/`SseEvent`. One `_es=EventSource('/api/sse',{withCredentials:true})`; `_handlers:Set`; auto-reopen via `queueMicrotask(_open)` on error if handlers remain. |
| client/src/pages/Home.tsx | `Home` (default). Top-level authed page. Wires `useBuckets`, `useInbox`, `useInboxSse`; owns `filterSelection`/`showView`/`showNew` state, `createWithWatchdog`, reclassify watchdog, `hydrateCurrentPage` on page change. Renders header, `<SecondaryHeader>`, `<InboxList>`, modals. |
| client/src/pages/inbox/useInbox.tsx | `useInbox({buckets,filterSelection})`. Owns inbox id/display layers, pagination, auto-extend, LWW gate. (details below). `PAGE_SIZE=50`, `SNAPSHOT_LIMIT=200`, `EXTEND_TIMEOUT_MS=90_000`, `UNCLASSIFIED='unclassified'`. |
| client/src/pages/inbox/useInboxSse.tsx | `useInboxSse({onApply,snapshot})`. Subscribes SSE; on `_open` runs snapshot→buffer-drain lifecycle; buffers `threads_updated` until `ready`; on `_error` clears `ready`. |
| client/src/pages/inbox/InboxList.tsx | `InboxList({threads,bucketsById})`. Grid rows (bucket pill, from, subject, preview). Helpers `abbreviate`, `BucketPill`. Empty→"syncing your inbox…". |
| client/src/pages/inbox/Pagination.tsx | `Pagination({page,pageCount,extending,onChange})`. prev/next + "page N of M" + "loading more…" when extending. Returns null when `pageCount<=1 && !extending`. |
| client/src/pages/inbox/ReloadButton.tsx | `ReloadButton({onResync})`. Click fires `requestRefresh()` (202 kick) AND `onResync()` in parallel; ~1.5s "syncing…" busy state. |
| client/src/pages/buckets/useBuckets.tsx | `useBuckets()`. State `buckets`/`loading`; `getBuckets()` on mount. Exports `buckets`, `byId`, `customBuckets`, `refresh`, `create`/`rename`/`softDelete` (each call api then `refresh()`). |
| client/src/pages/buckets/FilterByBucketDropdown.tsx | `FilterByBucketDropdown({buckets,selection,onChange})`. Multiselect incl. `UNCLASSIFIED`; `selection===null` means "all". Emits `null` when all selected. |
| client/src/pages/buckets/SecondaryHeader.tsx | `SecondaryHeader(...)`. Toolbar: `<ReloadButton>`, `<FilterByBucketDropdown>`, view/new bucket buttons, right-aligned `<Pagination>`. Pure prop pass-through from Home. |
| client/src/pages/buckets/NewBucketModal.tsx | `NewBucketModal({onClose,onSave})`. Steps `form|pending|review`. Owns `draftId`, `examples`, `appliedRef` (idempotency). SSE + 5s poll preview resolution. Helpers `ExampleRow`, `toExampleIn`, `Backdrop`. |
| client/src/pages/buckets/ViewBucketsModal.tsx | `ViewBucketsModal({buckets,onClose,onRename,onDelete})`. Inline rename (onBlur), delete with confirm copy (default buckets locked). |

## Routes / Tasks / Entrypoints
- **Routing** — `App.tsx`'s `Routes()` is the only router (no react-router): branches on `useAuth().state.status`. No URL paths beyond the `?authError` query read in `Login`.
- **useAuth** — owns auth context (`loading|authed|anon`). Bootstrap `useEffect`→`/auth/me`. `signOut`→`/auth/logout`.
- **useBuckets** — owns the canonical bucket list rendered everywhere (one instance, lives in `Home`). `create/rename/softDelete` mutate then `refresh()` (`GET /api/buckets`). `byId` map drives `InboxList` pills + filter.
- **useInbox** — owns inbox data model: `idLayer:string[]` (ordered ids) + `displayLayer:Record<id,InboxThread>` (rows), `page`, `more:boolean|null`, `extendInFlight`. Provides `snapshot` (loading toggle), `resync` (silent), `applyThreadUpdates(ids)`, `requestExtend`, `hydrateCurrentPage`, derived `pageThreads`/`pageCount`/`filteredIdLayer`.
- **useInboxSse** — bridges the SSE bus to `useInbox`: `_open`→`snapshot()`, then drains buffered `threads_updated` via `onApply`; live `threads_updated` apply directly once ready.
- **Modals** — `NewBucketModal` (form→pending→review draft flow), `ViewBucketsModal` (rename/soft-delete). Both shown conditionally from `Home`.
- **Key components** — `Home` (composition root), `SecondaryHeader` (toolbar), `InboxList`, `Pagination`, `ReloadButton`, `FilterByBucketDropdown`.

## Data & state touched
- **API endpoints via lib/api.ts** (all cookie-auth `same-origin`):
  - `GET /auth/me`, `POST /auth/logout` — useAuth.
  - `GET /api/inbox?page&limit` (`getInbox`, snapshot uses `limit=200`) — useInbox.
  - `POST /api/threads/batch {thread_ids}` (`getThreadsBatch`) — SSE replay / hydrate / extend apply.
  - `GET /api/threads/{id}` (`getThread`) — exported, **no current call site**.
  - `POST /api/inbox/refresh` (`requestRefresh`, 202) — ReloadButton.
  - `POST /api/inbox/extend {before_internal_date}` (`postInboxExtend`, 202) — useInbox auto-extend.
  - `GET /api/buckets`, `POST /api/buckets`, `PATCH /api/buckets/{id}`, `DELETE /api/buckets/{id}` (204) — useBuckets.
  - `POST /api/buckets/draft/preview {name,description,exclude_thread_ids}` (202→`{draft_id}`) + `GET /api/buckets/draft/preview/{draft_id}` (202 pending / 200 ready / 404 gone) — NewBucketModal.
- **SSE — lib/sse.ts** `EventSource('/api/sse')`. Data events consumed: `threads_updated{thread_ids}` (useInboxSse→useInbox.applyThreadUpdates), `extend_complete{thread_ids,more}` (useInbox), `bucket_draft_preview{draft_id,positives,near_misses}` (NewBucketModal). Synthetic conn events `_open`/`_error` broadcast to all handlers. Server `: keepalive` frames (5s) are non-JSON and ignored by `onmessage` try/catch.
- **Client-side state**: pagination cursor `page` + derived `pageCount` (PAGE_SIZE=50); `more` (history exhaustion); `extendInFlight`; refs `lastInternalDate` (LWW gate per id), `lastExtendAtLength` (no-progress guard), `extendWatchdog` (90s timer); NewBucketModal `appliedRef` (per-draft idempotency); Home reclassify `setTimeout`s (60s+150s).
- **fetch vs SSE**: snapshot/hydrate/mutations are fetch; new/changed threads, extend completion, and draft-preview results are pushed over SSE (with HTTP polling fallback only for draft preview).

## Data flows / cross-subsystem touchpoints
- `Browser ──[GET /auth/me, Cookie:session]──> API` — auth bootstrap.
- `Browser ──[GET /api/inbox?limit=200]──> API ──[JSON InboxPage]──> Browser` — snapshot/resync top-N view.
- `Browser ──[GET /api/sse, text/event-stream]──> API` — long-lived; frames `threads_updated`/`extend_complete`/`bucket_draft_preview` + `: keepalive`/5s.
- `Browser ──[POST /api/threads/batch {thread_ids:[…]}]──> API ──[JSON {threads}]──> Browser` — SSE replay/hydrate path (1 round trip for ≤200 ids).
- `Browser ──[POST /api/inbox/refresh]──> API` (202) — kicks a worker Gmail poll; results return later as SSE `threads_updated`. ReloadButton also resyncs locally.
- `Browser ──[POST /api/inbox/extend {before_internal_date}]──> API` (202) — worker extends Gmail history; completion arrives as SSE `extend_complete`.
- `Browser ──[POST /api/buckets/draft/preview]──> API ──[{draft_id}]──> Browser`; result via SSE `bucket_draft_preview` OR polled `GET /api/buckets/draft/preview/{draft_id}` (redis cache, 600s TTL).
- `Browser ──[POST /api/buckets]──> API` enqueues `reclassify_user_inbox`; completion *should* arrive as `threads_updated` but Home schedules 60s+150s `resync()` watchdogs to compensate.
- **Intra-tab (§2.11)**: `useInboxSse ↔ useInbox` and `NewBucketModal` all subscribe the single `lib/sse.ts` `EventSource` via the `_handlers` Set — one socket fans out to many React hooks.
- **SSE→state mutation**: `threads_updated`→`getThreadsBatch(ids)`→LWW-gate→merge `displayLayer` + re-sort `idLayer` desc by `internal_date`. `extend_complete`→`setMore`+clear watchdog+apply ids. `bucket_draft_preview`→push examples + flip modal to `review`.

## Decision points & gotchas
- **Pagination + auto-extend** (useInbox): `useEffect` fires `requestExtend` when `page >= pageCount-1`, not while a filter is active (filter shrinks pageCount artificially), not when `more===false` or `extendInFlight`. `lastExtendAtLength` ref blocks re-firing at the same `idLayer.length` so a 0-result extend can't tight-loop; re-arms when new ids grow the length. Extend cursor = smallest `recent_message.internal_date` across `idLayer`.
- **Extend watchdog**: `setTimeout(90_000)`; if `extend_complete` never arrives, force-reset `extendInFlight` + clear no-progress guard (no auto-retry — surfaces server bugs). Cleared on event arrival or POST failure.
- **LWW gate** (`applyThreadUpdates`): accept incoming thread only if `recent_message.internal_date >= lastInternalDate[id]` (default 0). `fetchAndReplace` **resets** (not merges) `lastInternalDate` so a thread aged out of the latest-200 window can't keep a stale gate value.
- **Snapshot/replay ordering** (useInboxSse): on `_open`, buffer `threads_updated` until `snapshot()` resolves, then drain in order — prevents an early SSE event being overwritten by a later snapshot. `cancelled` flag guards against re-`_open` racing a prior lifecycle. `_error` clears `ready` so subsequent events buffer again.
- **resync vs snapshot**: `snapshot` toggles `loading` (flashes "loading…"); `resync` (ReloadButton, watchdogs, page-clamp recovery) does NOT — keeps the list on screen. `resync` collapses `idLayer` to latest 200, so a page-clamp `useEffect` snaps `page` back to `pageCount` to avoid a blank extended-history page.
- **Optimistic vs server-confirmed**: nothing optimistic — `useBuckets` mutations refetch (`GET /api/buckets`) before UI updates; inbox rows always come from server via fetch/batch.
- **Draft preview dual-resolution** (NewBucketModal): SSE fast path + 5s poll fallback (poll starts at 5s to let SSE win the ~40s scoring window). `appliedRef` per `draft_id` makes apply idempotent regardless of arrival order; `gone`(404 TTL-expired) just stops — user re-runs "find examples"/"more examples" (each re-issues `postBucketDraftPreview` with `exclude_thread_ids=seenIds`).
- **Single useBuckets instance**: `NewBucketModal` takes `onSave` from Home's `useBuckets` rather than calling `useBuckets()` itself — a second instance would refresh nobody's rendered list, leaving toolbar/filter stale.
- **StaticFiles / dev-proxy split**: prod — Vite builds into `server/app/static/` (`VITE_OUT_DIR` override), served by FastAPI `StaticFiles` + SPA catch-all (same origin, no proxy). Dev — Vite dev server `:5173` proxies `/auth` and `/api` to `http://localhost:8000` (`vite.config.ts`); `same-origin`/`withCredentials` cookies work because the proxy keeps one origin.
- **401 handling**: `getJSON`/`getThreadsBatch` throw `{kind:'unauthorized'}`; `useAuth.refresh` maps any error to `anon`. Other hooks (`useInbox`, `useBuckets`) do not specially route 401 — they log/swallow.
