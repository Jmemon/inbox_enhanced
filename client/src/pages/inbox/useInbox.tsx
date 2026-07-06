import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getInbox, getThreadsBatch, postInboxExtend, type Bucket, type InboxThread } from '../../lib/api'
import { subscribeSse } from '../../lib/sse'


const PAGE_SIZE = 50
// Initial paint: 200 threads (4 pages of 50). Auto-extend (below) pulls more
// from Gmail history as the user paginates near the end, using the oldest
// thread's recent-message timestamp as the cursor. Server caps at MAX_LIMIT
// (200) to match.
const SNAPSHOT_LIMIT = 200
const UNCLASSIFIED = 'unclassified'

type IdLayer = string[]
type DisplayLayer = Record<string, InboxThread>


export function useInbox(opts: {
  buckets: Bucket[]
  filterSelection: Set<string> | null
  autoExtendEnabled?: boolean
}) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [idLayer, setIdLayer] = useState<IdLayer>([])
  const [displayLayer, setDisplayLayer] = useState<DisplayLayer>({})
  const [page, setPage] = useState(1)
  const [more, setMore] = useState<boolean | null>(null)
  const [extendInFlight, setExtendInFlight] = useState(false)

  const lastInternalDate = useRef<Record<string, number>>({})
  // Mirrors the recent_message.id of whatever is currently in displayLayer,
  // kept in lockstep with lastInternalDate (both updated together on accept).
  // Lets applyThreadUpdates detect a moved recent_message pointer even when
  // the LWW date gate alone can't order the update (see widened accept
  // condition below).
  const lastRecentMessageId = useRef<Record<string, string | undefined>>({})
  // No-progress guard: remembers the idLayer.length at which we last fired an
  // auto-extend. Blocks a tight retry loop if the server replies with 0 new
  // ids (length unchanged → guard locked). Re-arms naturally when new ids land
  // because length grows past the stored value.
  const lastExtendAtLength = useRef<number | null>(null)
  // Watchdog timer for in-flight extends. If the SSE extend_complete event
  // doesn't arrive within EXTEND_TIMEOUT_MS, force-reset extendInFlight so
  // the UI doesn't sit on "loading more…" forever. Failure modes that this
  // recovers from include redis pubsub messages dropped during SSE flapping
  // and any other delivery-side issue.
  const extendWatchdog = useRef<ReturnType<typeof setTimeout> | null>(null)
  const EXTEND_TIMEOUT_MS = 90_000

  const clearExtendWatchdog = useCallback(() => {
    if (extendWatchdog.current) {
      clearTimeout(extendWatchdog.current)
      extendWatchdog.current = null
    }
  }, [])

  // Shared helper: pulls the canonical top-N inbox view from the server and
  // overwrites idLayer + displayLayer. Used by both the kickoff snapshot
  // (which wants the "loading…" placeholder) and the user-triggered reload
  // (which must NOT blank the list — see resync below).
  const fetchAndReplace = useCallback(async () => {
    setError(null)
    const r = await getInbox({ limit: SNAPSHOT_LIMIT })
    const order: string[] = []
    const display: DisplayLayer = {}
    const nextDates: Record<string, number> = {}
    const nextRecentIds: Record<string, string | undefined> = {}
    for (const t of r.threads) {
      order.push(t.id); display[t.id] = t
      if (t.recent_message) nextDates[t.id] = t.recent_message.internal_date
      nextRecentIds[t.id] = t.recent_message?.id
    }
    // Reset, don't merge: an out-of-band thread we no longer surface (e.g. moved
    // out of the latest 200 window) should not keep a stale LWW gate value.
    lastInternalDate.current = nextDates
    lastRecentMessageId.current = nextRecentIds
    setIdLayer(order); setDisplayLayer(display)
  }, [])

  const snapshot = useCallback(async () => {
    setLoading(true)
    try { await fetchAndReplace() }
    catch (e: any) { setError(String(e?.kind ?? e?.message ?? e)) }
    finally { setLoading(false) }
  }, [fetchAndReplace])

  // Hard resync triggered by the user (reload button). Replaces idLayer +
  // displayLayer with the canonical server view WITHOUT toggling `loading` —
  // we don't want the rendered list to flash to "loading…" on every reload.
  // Errors are logged only; the existing list stays on screen.
  const resync = useCallback(async () => {
    try { await fetchAndReplace() }
    catch (e) { console.error('[useInbox] resync failed', e) }
  }, [fetchAndReplace])

  const applyThreadUpdates = useCallback(async (ids: string[]) => {
    if (ids.length === 0) return
    let fetched: InboxThread[] = []
    try { fetched = await getThreadsBatch(ids) } catch { return }

    // Partition `fetched` (not an LWW-gated `accepted` set) by is_archived
    // FIRST. is_archived is authoritative server state — evict unconditionally,
    // with no date gating. This ordering matters: a full delete (all messages
    // removed) sets is_archived=True with recent_message=null (incoming=0),
    // and a soft-delete of just the newest message also drops
    // recent_message.internal_date. Gating on date before checking
    // is_archived (the old bug) meant both cases could fail the `incoming >=
    // have` check and never reach eviction, leaving deleted content stuck on
    // screen.
    const archived = fetched.filter(t => t.is_archived)
    const live = fetched.filter(t => !t.is_archived)
    if (archived.length > 0) {
      const drop = new Set(archived.map(t => t.id))
      for (const t of archived) {
        delete lastInternalDate.current[t.id]
        delete lastRecentMessageId.current[t.id]
      }
      setDisplayLayer(prev => {
        const n = { ...prev }; for (const t of archived) delete n[t.id]; return n
      })
      setIdLayer(prev => prev.filter(id => !drop.has(id)))
    }

    // Live partition: keep the LWW gate (rejects stale/out-of-order batch
    // responses) but widen the accept condition. Accept when the incoming
    // date is newer-or-equal, OR when the recent_message pointer itself moved
    // (its id differs from what's currently displayed for this thread). The
    // id-difference bypass exists because a date-only comparison can't order
    // "pointer demotions": soft-deleting a thread's newest message lowers
    // recent_message.internal_date below what we already have, so a plain
    // `incoming >= have` gate would reject the update and leave the deleted
    // message's preview stuck on screen even though the thread is still live.
    // On accept we update lastInternalDate.current to the incoming date even
    // when it's LOWER than before — that's the point of the demotion.
    // Residual risk (unchanged from before this fix): two in-flight batch
    // responses for the same thread can still interleave/race with each
    // other; this only fixes ordering against the last *displayed* state, not
    // concurrent fetches racing one another.
    const accepted: InboxThread[] = []
    for (const t of live) {
      const incoming = t.recent_message?.internal_date ?? 0
      const have = lastInternalDate.current[t.id] ?? 0
      const pointerMoved = t.recent_message?.id !== lastRecentMessageId.current[t.id]
      if (incoming >= have || pointerMoved) {
        accepted.push(t)
        lastInternalDate.current[t.id] = incoming
        lastRecentMessageId.current[t.id] = t.recent_message?.id
      }
    }
    if (accepted.length === 0) return

    setDisplayLayer(prev => { const n = { ...prev }; for (const t of accepted) n[t.id] = t; return n })
    setIdLayer(prev => {
      const merged = new Set(prev); for (const t of accepted) merged.add(t.id)
      return [...merged].sort((a, b) =>
        (lastInternalDate.current[b] ?? 0) - (lastInternalDate.current[a] ?? 0))
    })
  }, [])

  // Subscribe to extend_complete to update `more` and hydrate the new ids.
  useEffect(() => {
    return subscribeSse((e) => {
      if (e.event !== 'extend_complete') return
      clearExtendWatchdog()
      setMore(e.more)
      setExtendInFlight(false)
      void applyThreadUpdates(e.thread_ids)
    })
  }, [applyThreadUpdates, clearExtendWatchdog])

  const requestExtend = useCallback(async () => {
    if (extendInFlight || more === false) return
    if (idLayer.length === 0) return
    let smallest = Number.MAX_SAFE_INTEGER
    for (const id of idLayer) {
      const t = displayLayer[id]
      const d = t?.recent_message?.internal_date
      if (d && d < smallest) smallest = d
    }
    if (smallest === Number.MAX_SAFE_INTEGER) return
    setExtendInFlight(true)
    // Watchdog: if extend_complete doesn't arrive within the timeout, force-
    // reset the in-flight flag and clear the no-progress guard so the user
    // can re-trigger by paginating. We don't auto-retry — that would mask
    // the server-side bug.
    clearExtendWatchdog()
    extendWatchdog.current = setTimeout(() => {
      console.warn('[useInbox] extend timed out without SSE event; resetting flag')
      setExtendInFlight(false)
      lastExtendAtLength.current = null
      extendWatchdog.current = null
    }, EXTEND_TIMEOUT_MS)
    try {
      await postInboxExtend(smallest)
    } catch {
      clearExtendWatchdog()
      setExtendInFlight(false)
    }
  }, [extendInFlight, more, idLayer, displayLayer, clearExtendWatchdog])

  // Filtered id layer: walk idLayer in order, keep only ids whose displayLayer
  // row's resolved bucket key matches the active filter set.
  const filteredIdLayer = useMemo(() => {
    if (!opts.filterSelection) return idLayer
    const activeIds = new Set(opts.buckets.map(b => b.id))
    const sel = opts.filterSelection
    return idLayer.filter((id) => {
      const t = displayLayer[id]
      if (!t) return false
      const bid = t.bucket_id
      const key = (bid === null || !activeIds.has(bid)) ? UNCLASSIFIED : bid
      return sel.has(key)
    })
  }, [idLayer, displayLayer, opts.filterSelection, opts.buckets])

  const pageCount = Math.max(1, Math.ceil(filteredIdLayer.length / PAGE_SIZE))
  const pageThreads = useMemo(() => {
    const start = (page - 1) * PAGE_SIZE
    return filteredIdLayer.slice(start, start + PAGE_SIZE)
      .map(id => displayLayer[id]).filter(Boolean)
  }, [page, filteredIdLayer, displayLayer])

  // Auto-extend trigger: fires when the user reaches the page-before-last (or
  // the last page) so the next batch is in flight before they need it. Skipped
  // when the server says no more history, when an extend is already running,
  // or when a filter is active (filter can artificially shrink page count).
  // The lastExtendAtLength ref blocks re-firing at the same idLayer size, so a
  // 0-result extend doesn't loop.
  useEffect(() => {
    if (opts.autoExtendEnabled === false) return
    if (more === false || extendInFlight) return
    if (opts.filterSelection) return
    if (page < pageCount - 1) return
    if (lastExtendAtLength.current === idLayer.length) return
    lastExtendAtLength.current = idLayer.length
    void requestExtend()
  }, [page, pageCount, more, extendInFlight, opts.filterSelection, idLayer.length, requestExtend, opts.autoExtendEnabled])

  // Page clamp: after a resync collapses idLayer back to the latest 200, a user
  // who had paginated into extended history (page 5+) would otherwise see an
  // empty list. Snap them back to the last valid page instead.
  useEffect(() => {
    if (page > pageCount) setPage(pageCount)
  }, [page, pageCount])

  const hydrateCurrentPage = useCallback(async () => {
    const start = (page - 1) * PAGE_SIZE
    const ids = idLayer.slice(start, start + PAGE_SIZE)
    const missing = ids.filter(id => !(id in displayLayer))
    if (missing.length > 0) await applyThreadUpdates(missing)
  }, [page, idLayer, displayLayer, applyThreadUpdates])

  return {
    loading, error, idLayer, displayLayer, page, pageCount, pageThreads,
    setPage, snapshot, resync, applyThreadUpdates, hydrateCurrentPage,
    more, requestExtend, extendInFlight,
  }
}
