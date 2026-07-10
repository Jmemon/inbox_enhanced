import { useEffect, useState } from 'react'
import { InboxList } from './InboxList'
import { SecondaryHeader } from '../buckets/SecondaryHeader'
import { ViewBucketsModal } from '../buckets/ViewBucketsModal'
import { NewTaskWizard } from '../task/NewTaskWizard'
import { useInboxSearch } from '../search/useInboxSearch'
import { SearchBar } from '../search/SearchBar'
import { useInboxStore } from '../../state/InboxProvider'


export default function InboxPage() {
  const { buckets, inbox, filterSelection, setFilterSelection } = useInboxStore()
  const [showView, setShowView] = useState(false)
  const [showNew, setShowNew] = useState(false)

  const search = useInboxSearch()

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
      {/* Bucket creation goes through the jobs surface now (Phase 4.5 Task 6)
          — this is start-mode only (goal form -> startCreation -> close);
          the wizard never waits on backfill here. Reviewing the resulting
          draft_ready job and refreshing this page's bucket list once it's
          live are both handled elsewhere: JobsPanel's [Review] action (see
          AppShell.tsx) opens a separate review-mode wizard instance, and
          JobsProvider itself calls buckets.refresh() on a bucket-creation
          job's done transition (state/JobsProvider.tsx) — this component no
          longer needs an onCreated hook to bridge that gap. */}
      {showNew && <NewTaskWizard kind="bucket" onClose={() => setShowNew(false)} />}
    </>
  )
}
