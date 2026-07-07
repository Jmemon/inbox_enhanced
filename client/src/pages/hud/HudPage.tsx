import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getSyncStatus, type SyncStatus, type Task } from '../../lib/api'
import { InboxList } from '../inbox/InboxList'
import { SearchBar } from '../search/SearchBar'
import { useInboxSearch } from '../search/useInboxSearch'
import { useInboxStore } from '../../state/InboxProvider'
import { useTasksStore } from '../../state/TasksProvider'
import { NewTaskWizard } from '../task/NewTaskWizard'

const RECENT_COUNT = 10

function agoLabel(epochSecs: number | null): string {
  // Number.isNaN guard: `epochSecs` can arrive NaN via lastEventAgo below
  // when `Date.parse(iso)` fails to parse (a malformed/unexpected
  // `last_event_at` string) — without this, `s` itself becomes NaN and every
  // comparison below is false, so `s < 3600` falls through to the final
  // `${NaN}h ago` branch. Treat unparseable the same as no timestamp at all.
  if (epochSecs === null || Number.isNaN(epochSecs)) return 'never'
  const s = Math.max(0, Math.floor(Date.now() / 1000) - epochSecs)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

// last_event_at comes back as an ISO string (or null) from the API, unlike
// agoLabel's epoch-seconds contract (see agoLabel above, used elsewhere in
// this file against status.last_synced_at which IS epoch seconds) — convert
// here rather than change agoLabel's signature for its one other caller.
function lastEventAgo(iso: string | null): string {
  return agoLabel(iso === null ? null : Math.floor(Date.parse(iso) / 1000))
}

const STAGE_HISTOGRAM_MAX = 4

// Compact "applied 4 · screen 2 · onsite 1" line from a task's stage_counts.
// Preserves server-controlled insertion order (plain Object.entries — do not
// sort), truncates to the first STAGE_HISTOGRAM_MAX stages, and folds the
// rest into a trailing "+n more". stage_counts is typed required on
// TaskSummary but is guarded here anyway against older cached payloads or an
// SSE-race snapshot that predates the server sending it.
function stageHistogramLabel(stageCounts: Record<string, number> | undefined): string | null {
  const entries = Object.entries(stageCounts ?? {})
  if (entries.length === 0) return null
  const shown = entries.slice(0, STAGE_HISTOGRAM_MAX)
  const parts = shown.map(([stage, count]) => `${stage} ${count}`)
  const extra = entries.length - shown.length
  if (extra > 0) parts.push(`+${extra} more`)
  return parts.join(' · ')
}

// Mirrors TaskDetail.tsx's statusChipStyle (kept local rather than shared —
// this task's scope is HudPage.tsx only) — paused renders visibly dimmed
// relative to active, per the plan.
const statusChipStyle = (status: Task['status']) => ({
  display: 'inline-block', padding: '2px 8px', borderRadius: 999, fontSize: 11, fontWeight: 500,
  background: status === 'active' ? '#e7f1ea' : '#f1eee7',
  color: status === 'active' ? '#2f6b46' : '#8a7a4b',
  opacity: status === 'active' ? 1 : 0.6,
})

export default function HudPage() {
  const { buckets, inbox } = useInboxStore()
  const { tasks, backfill } = useTasksStore()
  const search = useInboxSearch()
  const navigate = useNavigate()
  const [status, setStatus] = useState<SyncStatus | null>(null)
  const [, forceTick] = useState(0)
  const [showWizard, setShowWizard] = useState(false)

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
          <InboxList threads={search.results} bucketsById={buckets.byId} emptyLabel="no results" />
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
            <h2 style={{ fontSize: 14, margin: '0 0 8px' }}>Tasks</h2>
            {tasks.length === 0 && (
              <div style={{ fontSize: 13, color: '#888', marginBottom: 12 }}>
                No tasks yet — create one from a goal.
              </div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 12 }}>
              {tasks.map(task => {
                const taskBackfill = backfill[task.id]
                const stageHistogram = stageHistogramLabel(task.summary.stage_counts)
                return (
                  <div
                    key={task.id}
                    role="button"
                    tabIndex={0}
                    onClick={() => navigate(`/tasks/${task.id}`)}
                    onKeyDown={e => {
                      // Enter/Space activate the card the same way a click
                      // does — standard `role="button"` keyboard contract —
                      // so keyboard/screen-reader users can reach task
                      // detail without a mouse. Space also scrolls the page
                      // by default; preventDefault stops that.
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        navigate(`/tasks/${task.id}`)
                      }
                    }}
                    style={{ border: '1px solid #eee', borderRadius: 8, padding: 12, cursor: 'pointer', display: 'flex', flexDirection: 'column', gap: 6 }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                      <div style={{ fontWeight: 600, fontSize: 14 }}>{task.name}</div>
                      <span style={statusChipStyle(task.status)}>{task.status}</span>
                    </div>
                    <div style={{ fontSize: 12, color: '#888' }}>{task.summary.entities} entities</div>
                    {stageHistogram && <div style={{ fontSize: 11, color: '#aaa' }}>{stageHistogram}</div>}
                    {task.summary.pending_reviews > 0 && (
                      <span style={{
                        display: 'inline-block', alignSelf: 'flex-start', padding: '2px 8px', borderRadius: 999,
                        fontSize: 11, fontWeight: 500, background: '#fef3c7', color: '#92400e',
                      }}>
                        {task.summary.pending_reviews} to review
                      </span>
                    )}
                    <div style={{ fontSize: 11, color: '#888' }}>last event {lastEventAgo(task.summary.last_event_at)}</div>
                    {taskBackfill && !taskBackfill.done && (
                      <div style={{ fontSize: 11, color: '#888', fontStyle: 'italic' }}>
                        backfilling — scanned {taskBackfill.scanned} · matched {taskBackfill.matched}
                      </div>
                    )}
                  </div>
                )
              })}
              <button
                onClick={() => setShowWizard(true)}
                style={{
                  border: '1px dashed #ccc', borderRadius: 8, padding: 12, background: 'none', cursor: 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 14, color: '#666',
                  minHeight: 72,
                }}
              >
                + New task
              </button>
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
      {showWizard && <NewTaskWizard onClose={() => setShowWizard(false)} />}
    </>
  )
}
