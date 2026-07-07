import type { TaskEntity, TaskEvent } from '../../lib/api'

// PLACEHOLDER — Task 8 (plans/2026-07-06-phase2b-task-ui.md) replaces this
// file's body with the real review tray: human-readable pending_reason copy,
// evidence blockquotes, a "new entity?" hint via proposed_entity, and the
// full "recent activity" list. The prop shapes below are the Task 6/8
// contract — TaskDetail already wires real callbacks (approve/reject/revert
// → api call + refetch) against them, so keep the shapes stable across the
// rewrite. This stub renders the pending queue + a trimmed activity list so
// the wiring is exercised end to end before Task 8 lands.
export function ReviewFeed({ events, entitiesById, onApprove, onReject, onRevert }: {
  events: TaskEvent[]
  entitiesById: Record<string, TaskEntity>
  onApprove: (eventId: string) => void
  onReject: (eventId: string) => void
  onRevert: (eventId: string) => void
}) {
  const pending = events.filter((e) => e.status === 'pending_review')
  const recent = events.filter((e) => e.status !== 'pending_review').slice(0, 30)

  const entityLabel = (e: TaskEvent) =>
    (e.entity_id && entitiesById[e.entity_id]?.display_name) || e.proposed_entity || '(unknown entity)'

  return (
    <div style={{ border: '1px solid #eee', borderRadius: 8, padding: 16, display: 'grid', gap: 16 }}>
      <div>
        <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>Needs review ({pending.length})</div>
        {pending.length === 0 && <div style={{ color: '#888', fontSize: 13 }}>nothing pending</div>}
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 8 }}>
          {pending.map((e) => (
            <li key={e.id} style={{ border: '1px solid #eee', borderRadius: 6, padding: 8, fontSize: 13 }}>
              <div style={{ fontWeight: 600 }}>{entityLabel(e)}</div>
              <div style={{ color: '#666' }}>{e.field ?? '—'}: {e.old_value ?? '—'} → {e.new_value ?? '—'}</div>
              {e.pending_reason && <div style={{ color: '#a06a00', fontSize: 11, marginTop: 2 }}>{e.pending_reason}</div>}
              <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                <button onClick={() => onApprove(e.id)} style={{ fontSize: 12 }}>approve</button>
                <button onClick={() => onReject(e.id)} style={{ fontSize: 12 }}>reject</button>
              </div>
            </li>
          ))}
        </ul>
      </div>
      <div>
        <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>Recent activity</div>
        {recent.length === 0 && <div style={{ color: '#888', fontSize: 13 }}>no activity yet</div>}
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 6 }}>
          {recent.map((e) => (
            <li key={e.id} style={{
              fontSize: 12, color: '#444', display: 'flex', alignItems: 'center',
              justifyContent: 'space-between', gap: 8,
            }}>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {entityLabel(e)} — {e.field ?? '—'}: {e.old_value ?? '—'} → {e.new_value ?? '—'} ({e.origin}, {e.status})
              </span>
              {e.status === 'applied' && (
                <button onClick={() => onRevert(e.id)} style={{ fontSize: 11 }}>revert</button>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
