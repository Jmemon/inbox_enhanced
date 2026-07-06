import { useCallback, useEffect, useRef, useState } from 'react'
import { useInbox } from './useInbox'
import { useInboxSse } from './useInboxSse'
import { InboxList } from './InboxList'
import { useBuckets } from '../buckets/useBuckets'
import { SecondaryHeader } from '../buckets/SecondaryHeader'
import { ViewBucketsModal } from '../buckets/ViewBucketsModal'
import { NewBucketModal } from '../buckets/NewBucketModal'
import { searchInbox, type InboxThread } from '../../lib/api'


export default function InboxPage() {
  const { buckets, byId: bucketsById, create, rename, softDelete } = useBuckets()
  const [filterSelection, setFilterSelection] = useState<Set<string> | null>(null)
  const [showView, setShowView] = useState(false)
  const [showNew, setShowNew] = useState(false)

  const inbox = useInbox({ buckets, filterSelection })
  useInboxSse({ onApply: inbox.applyThreadUpdates, snapshot: inbox.snapshot })

  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<InboxThread[] | null>(null)
  const [searchError, setSearchError] = useState<string | null>(null)
  // Monotonic request token: guards against an older in-flight search
  // response resolving after a newer one and clobbering its results.
  const searchSeq = useRef(0)

  // Debounced server search: /api/search (Postgres FTS). Empty query exits
  // search mode and restores the normal inbox list.
  useEffect(() => {
    const q = searchQuery.trim()
    if (!q) {
      searchSeq.current += 1
      setSearchResults(null); setSearchError(null); return
    }
    const seq = ++searchSeq.current
    const t = setTimeout(async () => {
      try {
        const r = await searchInbox(q)
        if (seq !== searchSeq.current) return
        setSearchResults(r.threads); setSearchError(null)
      } catch (e: any) {
        if (seq !== searchSeq.current) return
        // Also engage search mode on failure (not just setSearchError): if
        // this is the FIRST search attempt, searchResults is still null, and
        // the render branch below gates on `searchResults !== null` — without
        // this, a first-search failure would set searchError but never show
        // it, silently leaving the normal inbox list on screen.
        setSearchResults([]); setSearchError(String(e?.message ?? e))
      }
    }, 300)
    return () => clearTimeout(t)
  }, [searchQuery])

  // Reclassify watchdog: POST /api/buckets enqueues reclassify_user_inbox,
  // which takes ~30-150s and publishes threads_updated when done. SSE
  // delivery during long-running tasks is unreliable (subscribers can churn
  // mid-task and pubsub is fire-and-forget), so schedule explicit resyncs at
  // 60s + 150s. resync() doesn't toggle the loading flag, so it doesn't flash
  // the list — it just merges the current server state into the display layer.
  const createWithWatchdog = useCallback(async (
    body: Parameters<typeof create>[0],
  ) => {
    const bucket = await create(body)
    setTimeout(() => { void inbox.resync() }, 60_000)
    setTimeout(() => { void inbox.resync() }, 150_000)
    return bucket
  }, [create, inbox])

  // Hydrate the current page when navigating to a page whose thread ids are
  // not yet in the display layer.
  useEffect(() => { void inbox.hydrateCurrentPage() /* eslint-disable-next-line */ }, [inbox.page])

  return (
    <>
      {/* SecondaryHeader owns the reload button, filter dropdown, bucket controls,
          and (right-aligned) the pagination row for the inbox list. */}
      <SecondaryHeader
        buckets={buckets} filterSelection={filterSelection}
        onFilterChange={setFilterSelection}
        onViewBuckets={() => setShowView(true)}
        onNewBucket={() => setShowNew(true)}
        page={inbox.page} pageCount={inbox.pageCount}
        extending={inbox.extendInFlight} onPageChange={inbox.setPage}
        onResync={inbox.resync}
      />

      <div style={{ padding: '8px 24px', borderBottom: '1px solid #eee' }}>
        <input
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="search your inbox…"
          style={{ width: 360, maxWidth: '100%', padding: '6px 10px', fontSize: 14 }}
        />
        {searchResults !== null && (
          <span style={{ marginLeft: 12, fontSize: 12, color: '#888' }}>
            {searchResults.length} result{searchResults.length === 1 ? '' : 's'}
            <button onClick={() => setSearchQuery('')}
                    style={{ marginLeft: 8, fontSize: 12 }}>clear</button>
          </span>
        )}
      </div>

      <main>
        {searchResults !== null ? (
          <>
            {searchError && <div style={{ color: '#8a1c25', padding: 16 }}>search error: {searchError}</div>}
            <InboxList threads={searchResults} bucketsById={bucketsById} />
          </>
        ) : (
          <>
            {inbox.error && <div style={{ color: '#8a1c25', padding: 16 }}>error: {inbox.error}</div>}
            {!inbox.error && inbox.loading && <div style={{ padding: 24 }}>loading…</div>}
            {!inbox.loading && <InboxList threads={inbox.pageThreads} bucketsById={bucketsById} />}
            {inbox.more === false && (
              <div style={{ padding: 12, fontSize: 12, color: '#888', textAlign: 'center' }}>
                (end of inbox history)
              </div>
            )}
          </>
        )}
      </main>

      {showView && <ViewBucketsModal buckets={buckets} onClose={() => setShowView(false)}
                                       onRename={rename} onDelete={softDelete} />}
      {showNew && <NewBucketModal onClose={() => setShowNew(false)} onSave={createWithWatchdog} />}
    </>
  )
}
