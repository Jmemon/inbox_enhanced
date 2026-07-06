import { useCallback, useEffect, useMemo, useState } from 'react'
import { getSyncStatus, type SyncStatus } from '../../lib/api'
import { InboxList } from '../inbox/InboxList'
import { SearchBar } from '../search/SearchBar'
import { useInboxSearch } from '../search/useInboxSearch'
import { useInboxStore } from '../../state/InboxProvider'

const RECENT_COUNT = 10

function agoLabel(epochSecs: number | null): string {
  if (epochSecs === null) return 'never'
  const s = Math.max(0, Math.floor(Date.now() / 1000) - epochSecs)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

export default function HudPage() {
  const { buckets, inbox } = useInboxStore()
  const search = useInboxSearch()
  const [status, setStatus] = useState<SyncStatus | null>(null)
  const [, forceTick] = useState(0)

  const refreshStatus = useCallback(async () => {
    try {
      const st = await getSyncStatus()
      setStatus(st)
    } catch (e) {
      console.error('[hud] refreshStatus failed', e)
    }
  }, [])

  // The inbox snapshot/live-updates now come from the shared InboxProvider
  // store (mounted in AppShell), so this page only needs its own status
  // machinery: an initial fetch, a 30s poll (empty polls mark last_sync
  // server-side without publishing an SSE event, so status must be polled
  // independently to stay truthful on quiet inboxes), and a 5s ticker so
  // "synced Ns ago" counts up between polls.
  useEffect(() => {
    void refreshStatus()
    const statusPoll = setInterval(() => { void refreshStatus() }, 30_000)
    const tick = setInterval(() => forceTick(n => n + 1), 5000)
    return () => { clearInterval(statusPoll); clearInterval(tick) }
  }, [refreshStatus])

  const recent = useMemo(
    () => inbox.idLayer.slice(0, RECENT_COUNT).map(id => inbox.displayLayer[id]).filter(Boolean),
    [inbox.idLayer, inbox.displayLayer],
  )

  const bucketCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const id of inbox.idLayer) {
      const t = inbox.displayLayer[id]
      if (t?.bucket_id) counts[t.bucket_id] = (counts[t.bucket_id] ?? 0) + 1
    }
    return counts
  }, [inbox.idLayer, inbox.displayLayer])

  return (
    <>
      <SearchBar query={search.query} setQuery={search.setQuery} results={search.results} />
      {search.results !== null ? (
        <main>
          {search.error && <div style={{ color: '#8a1c25', padding: 16 }}>search error: {search.error}</div>}
          <InboxList threads={search.results} bucketsById={buckets.byId} />
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
              <InboxList threads={recent} bucketsById={buckets.byId} />
            </div>
          </section>
          <section>
            <h2 style={{ fontSize: 14, margin: '0 0 8px' }}>Buckets</h2>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 12 }}>
              {buckets.buckets.map(b => (
                <div key={b.id} style={{ border: '1px solid #eee', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>{b.name}</div>
                  <div style={{ fontSize: 22, marginTop: 4 }}>{bucketCounts[b.id] ?? 0}</div>
                  <div style={{ fontSize: 11, color: '#888' }}>of latest {inbox.idLayer.length}</div>
                </div>
              ))}
            </div>
          </section>
        </main>
      )}
    </>
  )
}
