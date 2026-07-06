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

  const refreshStatus = useCallback(async () => {
    try {
      const st = await getSyncStatus()
      setStatus(st)
    } catch (e) {
      console.error('[hud] refreshStatus failed', e)
    }
  }, [])

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
    // Empty polls mark last_sync server-side without publishing an SSE event, so status must
    // be polled independently (every 30s, matching beat cadence) to stay truthful on quiet inboxes.
    const statusPoll = setInterval(() => { void refreshStatus() }, 30_000)
    return () => {
      unsub(); clearInterval(tick); clearInterval(statusPoll)
      if (refreshTimer.current) clearTimeout(refreshTimer.current)
    }
  }, [refresh, refreshStatus])

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
