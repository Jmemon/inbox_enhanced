# Phase 1 — Routing Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Spec: `specs/004_vision_arch/chosen-architecture.md` §3 Phase 1 + §6 (sync-status endpoint).
> Plan stamped at commit `cfbc8a7` on branch `main`.

**Goal:** Invert the SPA from inbox-only to HUD-first: `react-router-dom` with `/` = HUD skeleton (global search, sync-recency strip, bucket summary cards) and `/inbox` = today's UI verbatim, plus the one backend piece the HUD freshness strip needs — `GET /api/sync/status` backed by a Redis last-sync marker.

**Architecture:** Client-heavy phase. `App.tsx`'s auth state machine gains a `BrowserRouter`; an `AppShell` owns the top header/nav and — critically — a no-op SSE subscription that pins the module-level `EventSource` open across route changes (today it closes when its last subscriber unmounts, which would deregister the user from `active_users` and stop beat polling). `Home.tsx`'s content relocates to `pages/inbox/InboxPage.tsx` unchanged; its search logic is extracted into a shared hook so the HUD reuses it. Backend adds one tiny Redis marker module + one read-only endpoint. No migrations.

**Tech Stack:** React 19 / Vite / TypeScript (bun) + `react-router-dom` v7 (the one new dependency); FastAPI + Redis (existing).

## Global Constraints

- JS via `bun` only (`cd client && bun add react-router-dom` is the only dependency change); Python via `uv` only — no Python dependency changes in this phase.
- NEVER read `.env`. No new env vars.
- Server commands from `server/`: `cd server && uv run pytest -q 2>&1 | tail -5`. Client verification: `cd client && bun run build 2>&1 | tail -5` (no test runner exists; nothing under `server/app/static/` gets committed).
- Tests run on SQLite in-memory + fakeredis (follow `server/tests/test_active_users.py`'s redis-fake pattern for the marker tests and `test_inbox_api.py`'s authed-client pattern for the endpoint).
- Production read helpers never flush; repo/marker functions never commit.
- The FastAPI SPA catch-all (`app/main.py`) already serves `index.html` for non-`api/auth/assets` paths — deep links need ZERO backend routing work; do not touch the catch-all.
- `/inbox` must be byte-equivalent in behavior to today's `Home` (same hooks, same modals, same watchdogs); only the brand/user header moves up to `AppShell`.
- Commit after every task: `type(scope): summary`, no attribution lines.

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `server/app/realtime/last_sync.py` | create | Redis marker: `mark(user_id)` / `get(user_id)` for last successful sync epoch |
| `server/app/workers/tasks.py` | modify | `mark()` at each successful sync exit (poll ×3 paths, full sync, reclassify) |
| `server/app/api/sync.py` | create | `GET /api/sync/status` |
| `server/app/main.py` | modify | register sync router |
| `server/tests/test_sync_status.py` | create | marker + endpoint tests |
| `client/src/App.tsx` | modify | auth gate → `BrowserRouter` + route table |
| `client/src/AppShell.tsx` | create | header/nav/sign-out + SSE-pinning subscription + `<Outlet>` |
| `client/src/pages/inbox/InboxPage.tsx` | create | today's `Home` content minus the top header |
| `client/src/pages/Home.tsx` | delete | replaced by InboxPage + AppShell |
| `client/src/pages/search/useInboxSearch.ts` | create | debounced + race-guarded search state (extracted from Home) |
| `client/src/pages/search/SearchBar.tsx` | create | input + result count + clear button |
| `client/src/pages/hud/HudPage.tsx` | create | HUD skeleton: search, sync strip, recent threads, bucket cards |
| `client/src/lib/api.ts` | modify | `SyncStatus` type + `getSyncStatus()` |
| `reference/CLIENT_INDEX.md`, `reference/WORKERS_INDEX.md`, `reference/INBOX_SYNC_INDEX.md`, `reference/MANIFEST.md` | modify | re-index + re-stamp |

---

### Task 1: Last-sync marker + `GET /api/sync/status`

**Files:**
- Create: `server/app/realtime/last_sync.py`
- Modify: `server/app/workers/tasks.py` (five one-line `mark()` calls)
- Create: `server/app/api/sync.py`
- Modify: `server/app/main.py` (import + include)
- Test: `server/tests/test_sync_status.py`

**Interfaces:**
- Produces: `last_sync.mark(user_id: str) -> None` (SET `last_sync:{uid}` = epoch seconds, no TTL); `last_sync.get(user_id: str) -> int | None`; route `GET /api/sync/status` → `{"last_synced_at": int | null, "has_cursor": bool}`. Task 4 (HUD) consumes the route.

- [ ] **Step 1: Write failing tests**

Create `server/tests/test_sync_status.py`. Open `server/tests/test_active_users.py` and copy its fakeredis wiring exactly (it monkeypatches the realtime redis client); open `server/tests/test_inbox_api.py` and copy its authed-client fixture pattern for the endpoint test. Tests:

```python
def test_mark_then_get_roundtrip(fake_redis):
    from app.realtime import last_sync
    assert last_sync.get("u1") is None
    last_sync.mark("u1")
    got = last_sync.get("u1")
    assert isinstance(got, int) and got > 0


def test_sync_status_endpoint_shape(authed_client_with_user):
    # user seeded WITHOUT gmail_last_history_id; no mark() called
    r = client.get("/api/sync/status")
    assert r.status_code == 200
    assert r.json() == {"last_synced_at": None, "has_cursor": False}


def test_sync_status_reflects_mark_and_cursor(authed_client_with_user):
    # set user.gmail_last_history_id = "123" (flush), call last_sync.mark(user.id)
    r = client.get("/api/sync/status")
    body = r.json()
    assert body["has_cursor"] is True
    assert isinstance(body["last_synced_at"], int)
```

(Adapt fixture names to what those two files actually define — mirror an existing test in each; the assertions above are the contract. Unauthenticated request → 401 follows from `get_current_user`; add that assertion too.)

- [ ] **Step 2: Run to verify failure**

Run: `cd server && uv run pytest tests/test_sync_status.py -q 2>&1 | tail -5`
Expected: FAIL (`app.realtime.last_sync` missing).

- [ ] **Step 3: Implement the marker module**

Create `server/app/realtime/last_sync.py`:

```python
"""Per-user last-successful-sync marker (Redis, no TTL).

Written by the sync Celery tasks on every successful completion; read by
GET /api/sync/status to power the HUD freshness strip ("synced 12s ago").
Deliberately a marker, not a log — one key per user, overwritten in place.
"""

import time
from app.realtime import redis_client


def _key(user_id: str) -> str:
    return f"last_sync:{user_id}"


def mark(user_id: str) -> None:
    redis_client.get_redis().set(_key(user_id), int(time.time()))


def get(user_id: str) -> int | None:
    v = redis_client.get_redis().get(_key(user_id))
    return int(v) if v is not None else None
```

- [ ] **Step 4: Implement the endpoint + register**

Create `server/app/api/sync.py`:

```python
"""GET /api/sync/status — feeds the HUD freshness strip.

last_synced_at: epoch seconds of the user's last successful sync task
(null before first sync or if Redis lost the marker — the client renders
"never" / "unknown"). has_cursor: whether incremental sync is established.
"""

from fastapi import APIRouter, Depends
from app.db.models import User
from app.deps import get_current_user
from app.realtime import last_sync

router = APIRouter(prefix="/api", tags=["sync"])


@router.get("/sync/status")
def sync_status(user: User = Depends(get_current_user)) -> dict:
    return {
        "last_synced_at": last_sync.get(user.id),
        "has_cursor": bool(user.gmail_last_history_id),
    }
```

In `server/app/main.py`: `from app.api.sync import router as sync_router` and `app.include_router(sync_router)` alongside the existing includes.

- [ ] **Step 5: Mark at every successful sync exit**

In `server/app/workers/tasks.py`, add `from app.realtime import last_sync` and one `last_sync.mark(user_id)` immediately before each successful exit:

1. `poll_new_messages` — full-sync fallback path (no-cursor branch), right after `_publish_thread_ids(user_id, ids)`.
2. `poll_new_messages` — HistoryGone recovery path, same placement.
3. `poll_new_messages` — the empty-records silent return (a successful check IS a sync: `last_sync.mark(user_id)` then `return`), and after the partial-sync publish.
4. `full_sync_inbox_task` — after its `_publish_thread_ids`.
5. `reclassify_user_inbox` — after its `_publish_thread_ids` (it inline-reloads, so it counts).

Do NOT mark in `extend_inbox_history_task` (extends pull old mail; they say nothing about freshness).

- [ ] **Step 6: Run tests**

Run: `cd server && uv run pytest tests/test_sync_status.py tests/test_tasks.py -q 2>&1 | tail -5` → PASS.
Full suite: `cd server && uv run pytest -q 2>&1 | tail -5` → all green.

- [ ] **Step 7: Commit**

```bash
git add server/app/realtime/last_sync.py server/app/api/sync.py server/app/main.py server/app/workers/tasks.py server/tests/test_sync_status.py
git commit -m "feat(sync): last-sync marker + GET /api/sync/status"
```

---

### Task 2: Router + AppShell + InboxPage relocation

**Files:**
- Modify: `client/package.json` via `bun add react-router-dom`
- Modify: `client/src/App.tsx`
- Create: `client/src/AppShell.tsx`
- Create: `client/src/pages/inbox/InboxPage.tsx`
- Delete: `client/src/pages/Home.tsx`
- Create (placeholder until Task 4): `client/src/pages/hud/HudPage.tsx`

**Interfaces:**
- Consumes: existing `useAuth`, `subscribeSse` (`client/src/lib/sse.ts` — module singleton that closes when `_handlers` empties; the shell's subscription pins it).
- Produces: routes `/` (HUD), `/inbox`, `*`→`/`; `AppShell` renders `<Outlet/>`; `InboxPage` default-exports today's Home content minus the brand/user header.

- [ ] **Step 1: Add the dependency**

Run: `cd client && bun add react-router-dom`
Expected: installs v7.x; only `package.json` + `bun.lock` change.

- [ ] **Step 2: Create `AppShell.tsx`**

```tsx
import { useEffect } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from './auth/useAuth'
import { subscribeSse } from './lib/sse'

const navStyle = ({ isActive }: { isActive: boolean }) => ({
  fontSize: 13, padding: '4px 10px', borderRadius: 6, textDecoration: 'none',
  color: isActive ? '#111' : '#666', background: isActive ? '#eef2f7' : 'transparent',
  fontWeight: isActive ? 600 : 400,
})

export function AppShell() {
  const { state, signOut } = useAuth()

  // Pin the SSE singleton open for the life of the authed shell. lib/sse.ts
  // closes the EventSource when its LAST handler unsubscribes; without this,
  // navigating to a route that mounts no inbox hooks would close the stream,
  // deregister the user from active_users, and stop beat polling.
  useEffect(() => subscribeSse(() => {}), [])

  if (state.status !== 'authed') return null
  return (
    <div style={{ fontFamily: 'system-ui, sans-serif', minHeight: '100vh' }}>
      <header style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '12px 24px', borderBottom: '1px solid #eee',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
          <div style={{ fontWeight: 600 }}>inbox concierge</div>
          <nav style={{ display: 'flex', gap: 6 }}>
            <NavLink to="/" end style={navStyle}>HUD</NavLink>
            <NavLink to="/inbox" style={navStyle}>Inbox</NavLink>
          </nav>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 14, color: '#444' }}>{state.user.name ?? state.user.email}</span>
          <button onClick={signOut} style={{ fontSize: 13, padding: '6px 10px' }}>sign out</button>
        </div>
      </header>
      <Outlet />
    </div>
  )
}
```

- [ ] **Step 3: Create `pages/inbox/InboxPage.tsx`**

Move the entire body of `client/src/pages/Home.tsx` EXCEPT the outer `<header>` block (lines rendering brand + user + sign out — now AppShell's job) and its `useAuth` destructure of `signOut`. Keep: `useBuckets`, `filterSelection` state, modal state, `useInbox`, `useInboxSse`, the reclassify watchdog `createWithWatchdog`, the hydrate effect, the search state + debounced effect + `searchSeq` guard, `SecondaryHeader`, the search bar `<div>`, the branched `<main>`, and both modals — all verbatim. Adjust relative imports (`../auth/useAuth` → keep only if `state` is still needed for the authed guard; it isn't — AppShell gates auth, so drop the `useAuth` import and the `if (state.status !== 'authed') return null` guard entirely). Wrap in `export default function InboxPage()`. The outermost `<div style={{ fontFamily... minHeight... }}>` also moves to AppShell — InboxPage's root is a fragment (`<>...</>`) containing `SecondaryHeader`, search bar, `<main>`, and modals.

Delete `client/src/pages/Home.tsx` after the move (`git rm`).

- [ ] **Step 4: Create the HudPage placeholder**

`client/src/pages/hud/HudPage.tsx` (Task 4 replaces this):

```tsx
export default function HudPage() {
  return <div style={{ padding: 24, color: '#666' }}>HUD — coming in Task 4</div>
}
```

- [ ] **Step 5: Rewrite `App.tsx`**

```tsx
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth/useAuth'
import Splash from './auth/Splash'
import Login from './auth/Login'
import { AppShell } from './AppShell'
import HudPage from './pages/hud/HudPage'
import InboxPage from './pages/inbox/InboxPage'

// Renamed from `Routes` — react-router-dom owns that name now.
function Gate() {
  const { state } = useAuth()
  if (state.status === 'loading') return <Splash />
  if (state.status === 'anon') return <Login />
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<HudPage />} />
          <Route path="/inbox" element={<InboxPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  )
}
```

- [ ] **Step 6: Build + behavioral check**

Run: `cd client && bun run build 2>&1 | tail -5` → clean.
Grep check: `rg "from './pages/Home'|pages/Home" client/src` → no hits.

- [ ] **Step 7: Commit**

```bash
git add client/package.json client/bun.lock client/src/App.tsx client/src/AppShell.tsx client/src/pages/hud/HudPage.tsx client/src/pages/inbox/InboxPage.tsx
git rm client/src/pages/Home.tsx 2>/dev/null || true
git commit -m "feat(client): react-router shell — / HUD, /inbox relocated, SSE pinned in AppShell"
```

---

### Task 3: Extract shared search (`useInboxSearch` + `SearchBar`)

**Files:**
- Create: `client/src/pages/search/useInboxSearch.ts`
- Create: `client/src/pages/search/SearchBar.tsx`
- Modify: `client/src/pages/inbox/InboxPage.tsx` (adopt both; delete the inlined logic)

**Interfaces:**
- Produces: `useInboxSearch(): { query: string; setQuery: (q: string) => void; results: InboxThread[] | null; error: string | null }` — 300ms debounce, monotonic `searchSeq` race guard, empty/whitespace query → `results = null` (exit search mode). `SearchBar({ query, setQuery, results })` renders the input + count + clear. Task 4 (HUD) consumes both.

- [ ] **Step 1: Create the hook**

`client/src/pages/search/useInboxSearch.ts` — lift the state/effect from InboxPage verbatim (it is the code Home.tsx carried; preserve the race-guard comments):

```ts
import { useEffect, useRef, useState } from 'react'
import { searchInbox, type InboxThread } from '../../lib/api'

// Debounced server search against /api/search with a monotonic request token:
// stale responses (an earlier query resolving after a later one) are dropped,
// and a late response can't resurrect search mode after the user cleared it.
export function useInboxSearch() {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<InboxThread[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const searchSeq = useRef(0)

  useEffect(() => {
    const q = query.trim()
    if (!q) {
      searchSeq.current++
      setResults(null); setError(null)
      return
    }
    const seq = ++searchSeq.current
    const t = setTimeout(async () => {
      try {
        const r = await searchInbox(q)
        if (seq !== searchSeq.current) return
        setResults(r.threads); setError(null)
      } catch (e: any) {
        if (seq !== searchSeq.current) return
        // Engage search mode so the error is visible even on a first search.
        setResults([]); setError(String(e?.message ?? e))
      }
    }, 300)
    return () => clearTimeout(t)
  }, [query])

  return { query, setQuery, results, error }
}
```

(If InboxPage's current inlined version differs textually — it shipped in Phase 0 with exactly this logic — the MOVED code wins; do not fork behavior.)

- [ ] **Step 2: Create `SearchBar.tsx`**

```tsx
import type { InboxThread } from '../../lib/api'

export function SearchBar({ query, setQuery, results }: {
  query: string
  setQuery: (q: string) => void
  results: InboxThread[] | null
}) {
  return (
    <div style={{ padding: '8px 24px', borderBottom: '1px solid #eee' }}>
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="search your inbox…"
        style={{ width: 360, maxWidth: '100%', padding: '6px 10px', fontSize: 14 }}
      />
      {results !== null && (
        <span style={{ marginLeft: 12, fontSize: 12, color: '#888' }}>
          {results.length} result{results.length === 1 ? '' : 's'}
          <button onClick={() => setQuery('')} style={{ marginLeft: 8, fontSize: 12 }}>clear</button>
        </span>
      )}
    </div>
  )
}
```

- [ ] **Step 3: Adopt in InboxPage**

Replace the inlined search state/effect/JSX with:

```tsx
const search = useInboxSearch()
```

`<SearchBar query={search.query} setQuery={search.setQuery} results={search.results} />` replaces the inline bar; the `<main>` branch keys off `search.results !== null` and renders `search.error` / `<InboxList threads={search.results} …/>` exactly as before. Remove the now-unused `searchInbox` import and `useRef` if orphaned.

- [ ] **Step 4: Build + commit**

Run: `cd client && bun run build 2>&1 | tail -5` → clean.

```bash
git add client/src/pages/search/ client/src/pages/inbox/InboxPage.tsx
git commit -m "refactor(client): extract useInboxSearch + SearchBar for reuse"
```

---

### Task 4: HUD skeleton page

**Files:**
- Modify: `client/src/lib/api.ts` (`SyncStatus` + `getSyncStatus`)
- Replace: `client/src/pages/hud/HudPage.tsx` (placeholder → real page)

**Interfaces:**
- Consumes: `GET /api/sync/status` (Task 1), `useInboxSearch`/`SearchBar` (Task 3), existing `getInbox`, `useBuckets`, `InboxList`, `subscribeSse`.

- [ ] **Step 1: api.ts additions**

```ts
export type SyncStatus = { last_synced_at: number | null; has_cursor: boolean }

export function getSyncStatus(): Promise<SyncStatus> {
  return getJSON<SyncStatus>('/api/sync/status')
}
```

- [ ] **Step 2: Implement `HudPage.tsx`**

```tsx
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getInbox, getSyncStatus, type InboxThread, type SyncStatus } from '../../lib/api'
import { subscribeSse } from '../../lib/sse'
import { useBuckets } from '../buckets/useBuckets'
import { InboxList } from '../inbox/InboxList'
import { SearchBar } from '../search/SearchBar'
import { useInboxSearch } from '../search/useInboxSearch'

const RECENT_COUNT = 10

function agoLabel(epochSecs: number | null): string {
  if (epochSecs === null) return 'never'
  const s = Math.max(0, Math.floor(Date.now() / 1000) - epochSecs)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

export default function HudPage() {
  const { buckets, byId } = useBuckets()
  const search = useInboxSearch()
  const [snapshot, setSnapshot] = useState<InboxThread[]>([])
  const [status, setStatus] = useState<SyncStatus | null>(null)
  const [, forceTick] = useState(0)
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [inbox, st] = await Promise.all([getInbox({ limit: 200 }), getSyncStatus()])
      setSnapshot(inbox.threads); setStatus(st)
    } catch (e) {
      console.error('[hud] refresh failed', e)
    }
  }, [])

  // Initial load + SSE-driven refresh (debounced 2s — one refetch per burst),
  // + a 5s ticker so "synced Ns ago" counts up between events.
  useEffect(() => {
    void refresh()
    const unsub = subscribeSse((e) => {
      if (e.event !== 'threads_updated' && e.event !== '_open') return
      if (refreshTimer.current) clearTimeout(refreshTimer.current)
      refreshTimer.current = setTimeout(() => { void refresh() }, 2000)
    })
    const tick = setInterval(() => forceTick(n => n + 1), 5000)
    return () => {
      unsub(); clearInterval(tick)
      if (refreshTimer.current) clearTimeout(refreshTimer.current)
    }
  }, [refresh])

  const bucketCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const t of snapshot) if (t.bucket_id) counts[t.bucket_id] = (counts[t.bucket_id] ?? 0) + 1
    return counts
  }, [snapshot])

  return (
    <>
      <SearchBar query={search.query} setQuery={search.setQuery} results={search.results} />
      {search.results !== null ? (
        <main>
          {search.error && <div style={{ color: '#8a1c25', padding: 16 }}>search error: {search.error}</div>}
          <InboxList threads={search.results} bucketsById={byId} />
        </main>
      ) : (
        <main style={{ padding: '16px 24px', display: 'grid', gap: 24 }}>
          <section>
            <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>
              synced {agoLabel(status?.last_synced_at ?? null)}
              {status && !status.has_cursor && ' · first sync pending'}
            </div>
            <h2 style={{ fontSize: 14, margin: '0 0 8px' }}>Recently processed</h2>
            <div style={{ border: '1px solid #eee', borderRadius: 8, overflow: 'hidden' }}>
              <InboxList threads={snapshot.slice(0, RECENT_COUNT)} bucketsById={byId} />
            </div>
          </section>
          <section>
            <h2 style={{ fontSize: 14, margin: '0 0 8px' }}>Buckets</h2>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 12 }}>
              {buckets.map(b => (
                <div key={b.id} style={{ border: '1px solid #eee', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>{b.name}</div>
                  <div style={{ fontSize: 22, marginTop: 4 }}>{bucketCounts[b.id] ?? 0}</div>
                  <div style={{ fontSize: 11, color: '#888' }}>of latest {snapshot.length}</div>
                </div>
              ))}
            </div>
          </section>
        </main>
      )}
    </>
  )
}
```

- [ ] **Step 3: Build**

Run: `cd client && bun run build 2>&1 | tail -5` → clean.

- [ ] **Step 4: End-to-end check (dev stack)**

Run `scripts/dev.sh`; verify: `/` shows the HUD (sync strip ticking, recent threads, bucket counts); search from the HUD works; `/inbox` is today's UI; navigating HUD↔Inbox keeps the SSE connection open (Network tab: the `/api/sse` request survives navigation); hard-reload on `/inbox` deep-link works (SPA catch-all); unknown path redirects to `/`.

- [ ] **Step 5: Commit**

```bash
git add client/src/lib/api.ts client/src/pages/hud/HudPage.tsx
git commit -m "feat(client): HUD skeleton — search, sync freshness strip, recent threads, bucket cards"
```

---

### Task 5: Reference docs refresh + stamps

**Files:**
- Modify: `reference/CLIENT_INDEX.md`, `reference/WORKERS_INDEX.md`, `reference/INBOX_SYNC_INDEX.md`, `reference/MANIFEST.md`

- [ ] **Step 1: Update the indexes**

Verify each claim against code before writing (dense-index style, match existing conventions):
- `CLIENT_INDEX.md`: router table (`/`, `/inbox`, `*`), `AppShell` (SSE-pinning subscription gotcha — why it exists), `InboxPage` relocation (Home.tsx gone), `pages/search/` hook + component, `HudPage` (snapshot-derived counts, 2s-debounced SSE refresh, 5s ticker), `getSyncStatus`.
- `WORKERS_INDEX.md`: `last_sync:{uid}` Redis key row (who writes it — the five mark sites; who reads it — `/api/sync/status`).
- `INBOX_SYNC_INDEX.md`: `GET /api/sync/status` route row; `last_sync` marker in the Redis-state table.
- `MANIFEST.md`: stamps for all three rows + its own top stamp.

- [ ] **Step 2: Stamp + commit**

Code committed first (Tasks 1–4), then: `git log -1 --format=%h` → set `<!-- stamp: <sha> (<branch>) | <date> -->` in all four files.

```bash
git add reference/
git commit -m "docs(reference): re-index client/workers/sync for phase 1 routing shell"
```

- [ ] **Step 3: Final verification**

`cd server && uv run pytest -q 2>&1 | tail -5` → green; `cd client && bun run build 2>&1 | tail -5` → clean.
