import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { approveEvent, getReviews, rejectEvent, type EventFeedItem, type FeedItem } from '../../lib/api'
import { useTasksStore } from '../../state/TasksProvider'
import { pendingReasonCopy } from '../task/pendingReasons'

const BUSY_TIMEOUT_MS = 10_000

// Entity name for a card: prefer the feed's resolved entity_display_name,
// fall back to the LLM's raw proposed_entity string for events proposing a
// brand-new entity that doesn't exist yet (still pending review), and
// finally an explicit "unknown" — mirrors ReviewFeed's EntityLabel, but
// FeedItem already carries the resolved name (see server's
// _serialize_feed_event) so there's no entitiesById lookup to do here.
function EntityLabel({ item }: { item: EventFeedItem }) {
  if (item.entity_display_name) return <span>{item.entity_display_name}</span>
  if (item.proposed_entity) {
    return (
      <span>
        {item.proposed_entity}
        <span style={{ marginLeft: 6, fontSize: 11, fontWeight: 400, color: '#a06a00' }}>new entity?</span>
      </span>
    )
  }
  return <span style={{ color: '#999' }}>unknown</span>
}

function EvidenceQuote({ quote }: { quote: string }) {
  return (
    <blockquote style={{
      margin: '4px 0 0', padding: '4px 8px', borderLeft: '3px solid #ddd',
      color: '#555', fontSize: 12, fontStyle: 'italic',
    }}>
      {quote}
    </blockquote>
  )
}

// Aggregated cross-task review tray for the HUD. Self-owned data: fetches
// getReviews() itself rather than reading from TasksProvider, since the
// unified feed (every pending_review event across all of the user's tasks)
// has no other consumer. Approve/reject write through the same
// approveEvent/rejectEvent calls TaskDetail uses (they're keyed on task_id,
// which every FeedItem carries) and then just refetch this tray directly —
// TasksProvider's own SSE convergence (task_updated) is what keeps the
// HUD's task cards/badges in sync, so this component never touches that
// store's state.
export function ReviewTray() {
  const { tasks } = useTasksStore()
  // getReviews() returns the full merged FeedItem union (events + proposed
  // actions, Phase 5) — this component still only renders event cards (T6
  // adds action cards to the tray), so every pending action card is filtered
  // out here rather than left for the render loop to trip over union fields
  // it doesn't have (action items carry no `field`/`entity_display_name`/…).
  const [items, setItems] = useState<FeedItem[]>([])
  const events = useMemo(
    () => items.filter((i): i is EventFeedItem => i.type === 'event'),
    [items],
  )

  const refetch = useCallback(async () => {
    try {
      const reviews = await getReviews()
      setItems(reviews)
    } catch (e) {
      console.error('[ReviewTray] refetch failed', e)
    }
  }, [])

  useEffect(() => { void refetch() }, [refetch])

  // Cross-task early-warning signal: a review approved/rejected from inside
  // a task's own ReviewFeed (or a backfill/sync landing a new pending event)
  // changes that task's summary.pending_reviews via the task_updated SSE
  // push, well before this component's own action-triggered refetch would
  // otherwise notice. Sum rather than compare per-task so any task's count
  // moving (up or down) is a single scalar change to key the effect on.
  const totalPending = useMemo(
    () => tasks.reduce((sum, t) => sum + t.summary.pending_reviews, 0),
    [tasks],
  )

  // Skip the initial mount: the effect above already fetches once on mount,
  // and totalPending's first render is not a "change" worth a second fetch.
  const mountedRef = useRef(false)
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true
      return
    }
    void refetch()
    // Deliberately keyed on totalPending alone (not `refetch`, stable via
    // useCallback's `[]` deps anyway) — this must fire only when the summed
    // count actually moves, not on every render, to avoid a refetch loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [totalPending])

  // Per-item "action in flight" state, keyed by event id — same rationale
  // and shape as ReviewFeed's busy tracking: a clicked id stays busy until
  // the next `items` snapshot (from a refetch) no longer contains it, with a
  // BUSY_TIMEOUT_MS fallback to recover a button whose request failed
  // outright (approve/reject reject their promise, no refetch follows, so
  // without the timeout the button would stay disabled until unmount).
  const [busy, setBusy] = useState<Set<string>>(new Set())
  const busyTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  useEffect(() => {
    const stillPending = new Set(events.map((i) => i.id))
    setBusy((prev) => {
      let changed = false
      const next = new Set<string>()
      for (const id of prev) {
        if (stillPending.has(id)) {
          next.add(id)
        } else {
          const timer = busyTimersRef.current.get(id)
          if (timer) {
            clearTimeout(timer)
            busyTimersRef.current.delete(id)
          }
          changed = true
        }
      }
      return changed ? next : prev
    })
    // Keyed on `events` identity (a fresh array only on an actual refetch) —
    // see ReviewFeed's identical comment on its own [events]-keyed effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events])

  // On unmount, clear all pending busy timeouts.
  useEffect(() => {
    return () => {
      for (const timer of busyTimersRef.current.values()) {
        clearTimeout(timer)
      }
      busyTimersRef.current.clear()
    }
  }, [])

  const markBusy = (id: string) => {
    setBusy((prev) => new Set(prev).add(id))
    const timer = setTimeout(() => {
      setBusy((prev) => {
        const next = new Set(prev)
        next.delete(id)
        return next.size === prev.size ? prev : next
      })
      busyTimersRef.current.delete(id)
    }, BUSY_TIMEOUT_MS)
    busyTimersRef.current.set(id, timer)
  }

  const handleApprove = (item: EventFeedItem) => {
    markBusy(item.id)
    void approveEvent(item.task_id, item.id)
      .then(() => refetch())
      .catch((e) => console.error('[ReviewTray] approve failed', e))
  }

  const handleReject = (item: EventFeedItem) => {
    markBusy(item.id)
    void rejectEvent(item.task_id, item.id)
      .then(() => refetch())
      .catch((e) => console.error('[ReviewTray] reject failed', e))
  }

  if (events.length === 0) {
    return (
      <section>
        <h2 style={{ fontSize: 14, margin: '0 0 8px' }}>Review</h2>
        <div style={{ fontSize: 13, color: '#888' }}>Nothing needs review</div>
      </section>
    )
  }

  return (
    <section>
      <h2 style={{ fontSize: 14, margin: '0 0 8px' }}>Needs review ({events.length})</h2>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 8 }}>
        {events.map((item) => {
          const isBusy = busy.has(item.id)
          const reason = pendingReasonCopy(item)
          return (
            <li key={item.id} style={{ border: '1px solid #eee', borderRadius: 6, padding: 8, fontSize: 13 }}>
              <Link to={`/tasks/${item.task_id}`} style={{ fontSize: 11, color: '#4b5563' }}>
                {item.task_name}
              </Link>
              <div style={{ fontWeight: 600 }}>
                <EntityLabel item={item} />
              </div>
              <div style={{ color: '#666' }}>
                {item.field ?? '—'}: {item.old_value ?? '—'} → {item.new_value ?? '—'}
              </div>
              {item.evidence_quote && <EvidenceQuote quote={item.evidence_quote} />}
              {reason && <div style={{ color: '#a06a00', fontSize: 11, marginTop: 2 }}>{reason}</div>}
              <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                <button
                  disabled={isBusy}
                  onClick={() => handleApprove(item)}
                  style={{ fontSize: 12 }}
                >
                  approve
                </button>
                <button
                  disabled={isBusy}
                  onClick={() => handleReject(item)}
                  style={{ fontSize: 12 }}
                >
                  reject
                </button>
              </div>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
