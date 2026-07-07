import { useEffect, useMemo, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import type { TaskEntity, TaskEvent, TaskStateSchema } from '../../lib/api'

// A right-side fixed panel rather than a NewTaskWizard-style centered
// Backdrop modal: a drawer reads better anchored to an edge next to the
// board it's inspecting (board + review feed stay visible/scrollable behind
// it), which a centered modal would fully obscure. Still reuses the
// click-outside-to-close idea from NewTaskWizard's Backdrop — a full-bleed
// transparent scrim sits behind the panel and calls onClose on click.
export function EntityDrawer({ taskId: _taskId, entity, schema, events, onClose, onEdit, onMerge, onRevert, allEntities }: {
  taskId: string
  entity: TaskEntity
  schema: TaskStateSchema
  events: TaskEvent[]
  onClose: () => void
  onEdit: (field: string, value: string) => Promise<void>
  onMerge: (intoEntityId: string) => void
  onRevert: (eventId: string) => void
  allEntities: TaskEntity[]
}) {
  const [error, setError] = useState<string | null>(null)
  const [mergeTarget, setMergeTarget] = useState('')

  // Reset transient UI state (inline error banner, merge-target selection)
  // whenever the selected entity changes — otherwise a stale 422 from
  // editing entity A would still be showing when the user opens entity B.
  useEffect(() => {
    setError(null)
    setMergeTarget('')
  }, [entity.id])

  const allStages = [...schema.pipeline.stages, ...schema.pipeline.terminal]
  const attributes = schema.entity?.attributes ?? []
  const mergeCandidates = allEntities.filter((e) => e.id !== entity.id)

  // This entity's own history, newest first. Filters the full task events
  // list itself (rather than requiring the caller to pre-filter) — same
  // convention as ReviewFeed, which filters/derives its own subsets from the
  // full events array TaskDetail owns.
  const entityEvents = useMemo(
    () => events
      .filter((e) => e.entity_id === entity.id)
      .slice()
      .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()),
    [events, entity.id],
  )

  // Shared commit path for every field edit (stage select, enum select, or
  // plain text input): clears any previous error, awaits the caller's
  // onEdit (TaskDetail's version calls setEntityState + refetches board and
  // events, and rethrows on failure), and on rejection renders the error
  // message inline — the schema validator's 422 detail is the whole reason
  // this is worth showing here instead of just console.error-ing it.
  async function commit(field: string, value: string) {
    setError(null)
    try {
      await onEdit(field, value)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <>
      <div onClick={onClose} style={scrimStyle} />
      <div style={panelStyle}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <h3 style={{ margin: 0, fontSize: 16 }}>{entity.display_name}</h3>
          <button onClick={onClose} style={{ fontSize: 13 }}>close</button>
        </div>

        {error && (
          <div style={{
            color: '#8a1c25', fontSize: 13, background: '#fdf0f1', padding: 8,
            borderRadius: 4, marginTop: 12,
          }}>
            {error}
          </div>
        )}

        <div style={{ marginTop: 16, display: 'grid', gap: 12 }}>
          <FieldRow label="stage">
            <select
              value={entity.state.stage ?? ''}
              onChange={(ev) => {
                const next = ev.target.value
                if (next && next !== (entity.state.stage ?? '')) void commit('stage', next)
              }}
              style={inputStyle}
            >
              <option value="" disabled>(no stage)</option>
              {allStages.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </FieldRow>

          {attributes.map((attr) => (
            <FieldRow key={attr.key} label={attr.key}>
              {attr.type === 'enum' ? (
                <select
                  value={entity.state[attr.key] ?? ''}
                  onChange={(ev) => {
                    const next = ev.target.value
                    if (next && next !== (entity.state[attr.key] ?? '')) void commit(attr.key, next)
                  }}
                  style={inputStyle}
                >
                  <option value="" disabled>(unset)</option>
                  {(attr.values ?? []).map((v) => <option key={v} value={v}>{v}</option>)}
                </select>
              ) : (
                // Uncontrolled, keyed on the field's current true value: React
                // only remounts (and re-seeds defaultValue) when the source of
                // truth changes underneath us — e.g. this drawer's own commit
                // triggers TaskDetail's refetch — not on every keystroke, so
                // in-progress typing survives re-renders but a successfully
                // committed value always wins over any stale local edit.
                <input
                  key={entity.state[attr.key] ?? ''}
                  defaultValue={entity.state[attr.key] ?? ''}
                  onBlur={(ev) => {
                    const next = ev.target.value
                    if (next !== (entity.state[attr.key] ?? '')) void commit(attr.key, next)
                  }}
                  style={inputStyle}
                />
              )}
            </FieldRow>
          ))}
        </div>

        {mergeCandidates.length > 0 && (
          <div style={{ marginTop: 20 }}>
            <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>merge into…</div>
            <div style={{ display: 'flex', gap: 6 }}>
              <select
                value={mergeTarget}
                onChange={(ev) => setMergeTarget(ev.target.value)}
                style={{ ...inputStyle, flex: 1 }}
              >
                <option value="">select an entity…</option>
                {mergeCandidates.map((e) => <option key={e.id} value={e.id}>{e.display_name}</option>)}
              </select>
              {/* No confirm() here — TaskDetail's onMerge handler owns the
                  window.confirm (it also owns closing the drawer + refetch
                  on success), so this fires the request directly. */}
              <button disabled={!mergeTarget} onClick={() => onMerge(mergeTarget)} style={{ fontSize: 13 }}>
                merge
              </button>
            </div>
          </div>
        )}

        <div style={{ marginTop: 20 }}>
          <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>history</div>
          {entityEvents.length === 0 && <div style={{ color: '#888', fontSize: 13 }}>no activity yet</div>}
          <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 8 }}>
            {entityEvents.map((e) => (
              <li key={e.id} style={{ border: '1px solid #eee', borderRadius: 6, padding: 8, fontSize: 12 }}>
                <div style={{ color: '#444' }}>
                  {e.field ?? '—'}: {e.old_value ?? '—'} → {e.new_value ?? '—'}
                  <span style={{ color: '#999' }}> ({e.origin}, {e.status})</span>
                </div>
                {e.evidence_quote && (
                  <blockquote style={{
                    borderLeft: '3px solid #ccc', margin: '6px 0', padding: '0 8px', color: '#666',
                  }}>
                    {e.evidence_quote}
                  </blockquote>
                )}
                {e.status === 'applied' && (
                  <button onClick={() => onRevert(e.id)} style={{ fontSize: 11, marginTop: 4 }}>revert</button>
                )}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </>
  )
}

function FieldRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, color: '#666' }}>
      <span>{label}</span>
      {children}
    </label>
  )
}

const scrimStyle: CSSProperties = {
  position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.25)', zIndex: 100,
}

const panelStyle: CSSProperties = {
  position: 'fixed', top: 0, right: 0, bottom: 0, width: 420, maxWidth: '90vw',
  background: '#fff', borderLeft: '1px solid #ddd', boxShadow: '-4px 0 16px rgba(0,0,0,0.08)',
  padding: 20, overflowY: 'auto', zIndex: 101,
}

const inputStyle: CSSProperties = {
  width: '100%', boxSizing: 'border-box', padding: '6px 8px', fontSize: 13,
  borderRadius: 4, border: '1px solid #ccc',
}
