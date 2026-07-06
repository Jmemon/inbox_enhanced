import { useCallback, useEffect, useState } from 'react'
import { InboxList } from './InboxList'
import { SecondaryHeader } from '../buckets/SecondaryHeader'
import { ViewBucketsModal } from '../buckets/ViewBucketsModal'
import { NewBucketModal } from '../buckets/NewBucketModal'
import { useInboxSearch } from '../search/useInboxSearch'
import { SearchBar } from '../search/SearchBar'
import { useInboxStore } from '../../state/InboxProvider'


export default function InboxPage() {
  const { buckets, inbox, filterSelection, setFilterSelection } = useInboxStore()
  const [showView, setShowView] = useState(false)
  const [showNew, setShowNew] = useState(false)

  const search = useInboxSearch()

  // Reclassify watchdog: POST /api/buckets enqueues reclassify_user_inbox,
  // which takes ~30-150s and publishes threads_updated when done. SSE
  // delivery during long-running tasks is unreliable (subscribers can churn
  // mid-task and pubsub is fire-and-forget), so schedule explicit resyncs at
  // 60s + 150s. resync() doesn't toggle the loading flag, so it doesn't flash
  // the list — it just merges the current server state into the display layer.
  const createWithWatchdog = useCallback(async (
    body: Parameters<typeof buckets.create>[0],
  ) => {
    const bucket = await buckets.create(body)
    setTimeout(() => { void inbox.resync() }, 60_000)
    setTimeout(() => { void inbox.resync() }, 150_000)
    return bucket
  }, [buckets.create, inbox])

  // Hydrate the current page when navigating to a page whose thread ids are
  // not yet in the display layer.
  useEffect(() => { void inbox.hydrateCurrentPage() /* eslint-disable-next-line */ }, [inbox.page])

  return (
    <>
      {/* SecondaryHeader owns the reload button, filter dropdown, bucket controls,
          and (right-aligned) the pagination row for the inbox list. */}
      <SecondaryHeader
        buckets={buckets.buckets} filterSelection={filterSelection}
        onFilterChange={setFilterSelection}
        onViewBuckets={() => setShowView(true)}
        onNewBucket={() => setShowNew(true)}
        page={inbox.page} pageCount={inbox.pageCount}
        extending={inbox.extendInFlight} onPageChange={inbox.setPage}
        onResync={inbox.resync}
      />

      <SearchBar query={search.query} setQuery={search.setQuery} results={search.results} />

      <main>
        {search.results !== null ? (
          <>
            {search.error && <div style={{ color: '#8a1c25', padding: 16 }}>search error: {search.error}</div>}
            <InboxList threads={search.results} bucketsById={buckets.byId} emptyLabel="no results" />
          </>
        ) : (
          <>
            {inbox.error && <div style={{ color: '#8a1c25', padding: 16 }}>error: {inbox.error}</div>}
            {!inbox.error && inbox.loading && <div style={{ padding: 24 }}>loading…</div>}
            {!inbox.loading && <InboxList threads={inbox.pageThreads} bucketsById={buckets.byId} />}
            {inbox.more === false && (
              <div style={{ padding: 12, fontSize: 12, color: '#888', textAlign: 'center' }}>
                (end of inbox history)
              </div>
            )}
          </>
        )}
      </main>

      {showView && <ViewBucketsModal buckets={buckets.buckets} onClose={() => setShowView(false)}
                                       onRename={buckets.rename} onDelete={buckets.softDelete} />}
      {showNew && <NewBucketModal onClose={() => setShowNew(false)} onSave={createWithWatchdog} />}
    </>
  )
}
