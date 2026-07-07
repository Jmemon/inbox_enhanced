import { useEffect, useRef, useState } from 'react'
import type { TaskEntity, TaskEvent } from '../../lib/api'
import { pendingReasonCopy } from './pendingReasons'

const BUSY_TIMEOUT_MS = 10_000

// Entity name for a card/row: prefer the resolved entity (its current
// display_name, post any merge/rename), fall back to the LLM's raw
// proposed_entity string for events proposing a brand-new entity that
// doesn't exist yet (still pending review), and finally an explicit
// "unknown" — events are user-facing, so this shouldn't silently render
// blank even in a state the schema doesn't expect.
function EntityLabel({ event, entitiesById }: { event: TaskEvent; entitiesById: Record<string, TaskEntity> }) {
  const known = event.entity_id ? entitiesById[event.entity_id] : undefined
  if (known) return <span>{known.display_name}</span>
  if (event.proposed_entity) {
    return (
      <span>
        {event.proposed_entity}
        <span style={{ marginLeft: 6, fontSize: 11, fontWeight: 400, color: '#a06a00' }}>new entity?</span>
      </span>
    )
  }
  return <span style={{ color: '#999' }}>unknown</span>
}

function originLabel(origin: TaskEvent['origin']): string {
  return origin === 'llm' ? 'LLM' : 'you'
}

function OriginBadge({ origin }: { origin: TaskEvent['origin'] }) {
  return (
    <span style={{
      display: 'inline-block', marginRight: 6, padding: '1px 6px', borderRadius: 999,
      fontSize: 10, fontWeight: 500,
      background: origin === 'llm' ? '#eef2f7' : '#e7f1ea',
      color: origin === 'llm' ? '#4b5563' : '#2f6b46',
    }}>
      {originLabel(origin)}
    </span>
  )
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

export function ReviewFeed({ events, entitiesById, onApprove, onReject, onRevert }: {
  events: TaskEvent[]
  entitiesById: Record<string, TaskEntity>
  onApprove: (eventId: string) => void
  onReject: (eventId: string) => void
  onRevert: (eventId: string) => void
}) {
  const pending = events.filter((e) => e.status === 'pending_review')
  // list_task_events already returns newest-first (task_engine/repo.py's
  // list_events orders by created_at desc) — filtering preserves that order,
  // so slicing 30 here is "newest 30 non-pending" without a re-sort.
  const recent = events.filter((e) => e.status !== 'pending_review').slice(0, 30)

  // Per-event "action in flight" state, keyed by event id. onApprove/onReject
  // are locked to a synchronous void signature (TaskDetail fires the API
  // call + refetch fire-and-forget — see its handleApprove/handleReject) so
  // there's no promise to await here. Instead, a clicked id stays busy until
  // the NEXT `events` snapshot arrives that no longer carries it as
  // pending_review — i.e. the refetch that follows a successful approve or
  // reject. A request that fails outright never refetches (TaskDetail just
  // console.errors it), so its button would stay stuck disabled. The parent's
  // handler swallows failures without a refetch, so a failed request would
  // otherwise disable the button until unmount; we add a 10-second timeout
  // fallback to recover from that.
  const [busy, setBusy] = useState<Set<string>>(new Set())
  const busyTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  useEffect(() => {
    const stillPending = new Set(pending.map((e) => e.id))
    setBusy((prev) => {
      let changed = false
      const next = new Set<string>()
      for (const id of prev) {
        if (stillPending.has(id)) {
          next.add(id)
        } else {
          // Event is no longer pending — clear any pending timeout for this id
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
    // Keyed on `events` identity (a fresh array only on an actual refetch),
    // not `pending` (a fresh array every render) — see comment above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events])

  // On unmount, clear all pending busy timeouts
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
    // Start a timeout that clears this id's busy state after BUSY_TIMEOUT_MS.
    // If the event-driven clear (useEffect on [events]) fires first, it will
    // clear this timer. If the request fails and no refetch occurs, this
    // timeout will restore the button to enabled after 10 seconds.
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

  return (
    <div style={{ border: '1px solid #eee', borderRadius: 8, padding: 16, display: 'grid', gap: 16, alignContent: 'start' }}>
      <div>
        <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>Needs review ({pending.length})</div>
        {pending.length === 0 && <div style={{ color: '#888', fontSize: 13 }}>nothing pending</div>}
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 8 }}>
          {pending.map((e) => {
            const isBusy = busy.has(e.id)
            const reason = pendingReasonCopy(e)
            return (
              <li key={e.id} style={{ border: '1px solid #eee', borderRadius: 6, padding: 8, fontSize: 13 }}>
                <div style={{ fontWeight: 600 }}>
                  <EntityLabel event={e} entitiesById={entitiesById} />
                </div>
                <div style={{ color: '#666' }}>
                  {e.field ?? '—'}: {e.old_value ?? '—'} → {e.new_value ?? '—'}
                </div>
                {e.evidence_quote && <EvidenceQuote quote={e.evidence_quote} />}
                {e.confidence !== null && (
                  <div style={{ color: '#888', fontSize: 11, marginTop: 4 }}>
                    confidence: {e.confidence}%
                  </div>
                )}
                {reason && <div style={{ color: '#a06a00', fontSize: 11, marginTop: 2 }}>{reason}</div>}
                <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                  <button
                    disabled={isBusy}
                    onClick={() => { markBusy(e.id); onApprove(e.id) }}
                    style={{ fontSize: 12 }}
                  >
                    approve
                  </button>
                  <button
                    disabled={isBusy}
                    onClick={() => { markBusy(e.id); onReject(e.id) }}
                    style={{ fontSize: 12 }}
                  >
                    reject
                  </button>
                </div>
              </li>
            )
          })}
        </ul>
      </div>
      <div>
        <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>Recent activity</div>
        {recent.length === 0 && <div style={{ color: '#888', fontSize: 13 }}>no activity yet</div>}
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 8 }}>
          {recent.map((e) => (
            <li key={e.id} style={{ fontSize: 12, color: '#444', display: 'grid', gap: 2 }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  <OriginBadge origin={e.origin} />
                  <EntityLabel event={e} entitiesById={entitiesById} /> — {e.field ?? '—'}: {e.old_value ?? '—'} → {e.new_value ?? '—'} ({e.status})
                </span>
                {e.status === 'applied' && (
                  <button onClick={() => onRevert(e.id)} style={{ fontSize: 11, flexShrink: 0 }}>revert</button>
                )}
              </div>
              {e.evidence_quote && <EvidenceQuote quote={e.evidence_quote} />}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
