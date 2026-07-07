import type { CSSProperties } from 'react'
import type { TaskEntity, TaskStateSchema } from '../../lib/api'

// Real board (replaces the Task 6 stub). Columns are the pipeline stages in
// schema order, followed by the terminal stages (visually dimmed — they read
// as "done" states, not somewhere you drag things around further), plus a
// leading "(no stage)" column for entities with no stage yet — or whose
// stage value doesn't match any column currently declared by the schema
// (e.g. a stage renamed out from under existing data by a schema edit) —
// that column is omitted entirely when there's nothing in it, per the plan.
//
// Singleton tasks (schema.entity === null) always have exactly one entity —
// entity_key SINGLETON_KEY ('_self'), see server/app/task_engine/schema.py's
// module docstring — so this same column layout already renders as a
// one-row board for them (one card total, sitting under whichever column
// matches its current stage); no special-cased rendering path is needed.
export function PipelineBoard({ schema, entities, onMove, onOpenEntity }: {
  schema: TaskStateSchema | null
  entities: TaskEntity[]
  onMove: (entityId: string, stage: string) => void
  onOpenEntity: (entityId: string) => void
}) {
  if (!schema) {
    return <div style={{ padding: 16, color: '#888', fontSize: 13 }}>schema pending…</div>
  }

  const stages = schema.pipeline.stages
  const terminal = schema.pipeline.terminal
  // "Move to" options include terminal stages directly (you can e.g. jump
  // straight to "rejected" without stepping through every interim stage).
  const allStages = [...stages, ...terminal]
  const known = new Set(allStages)

  const byStage = (stage: string) => entities.filter((e) => e.state.stage === stage)
  const noStage = entities.filter((e) => !e.state.stage || !known.has(e.state.stage))

  type Column = { key: string; label: string; dimmed: boolean; cards: TaskEntity[] }
  const columns: Column[] = [
    ...(noStage.length > 0 ? [{ key: '__no_stage__', label: '(no stage)', dimmed: false, cards: noStage }] : []),
    ...stages.map((s) => ({ key: s, label: s, dimmed: false, cards: byStage(s) })),
    ...terminal.map((s) => ({ key: s, label: s, dimmed: true, cards: byStage(s) })),
  ]

  return (
    <div style={{ border: '1px solid #eee', borderRadius: 8, padding: 16 }}>
      <div style={{ fontSize: 12, color: '#888', marginBottom: 12 }}>
        board — {entities.length} {entities.length === 1 ? 'entity' : 'entities'}
      </div>
      {entities.length === 0 ? (
        <div style={{ color: '#888', fontSize: 13 }}>no entities yet</div>
      ) : (
        <div style={{ display: 'flex', gap: 12, overflowX: 'auto' }}>
          {columns.map((col) => (
            <div key={col.key} style={{ minWidth: 200, flex: '0 0 auto', opacity: col.dimmed ? 0.6 : 1 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: col.dimmed ? '#999' : '#333', marginBottom: 8 }}>
                {col.label} ({col.cards.length})
              </div>
              <div style={{ display: 'grid', gap: 6 }}>
                {col.cards.map((e) => (
                  <EntityCard
                    key={e.id} entity={e} schema={schema} allStages={allStages}
                    onMove={onMove} onOpenEntity={onOpenEntity}
                  />
                ))}
                {col.cards.length === 0 && <div style={{ fontSize: 12, color: '#bbb' }}>—</div>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Up to 3 non-stage state fields for a card, in schema attribute order (not
// raw Object.keys() order, which just reflects whatever order the server's
// JSON happened to serialize) — falls back to the state's own keys for
// singleton tasks (schema.entity === null), which have no attribute list to
// order by. `k in entity.state` drops attributes the schema declares but
// this particular entity hasn't had extracted yet, rather than showing a
// blank/undefined row for them.
function nonStageFieldEntries(entity: TaskEntity, schema: TaskStateSchema): [string, string | null][] {
  const orderedKeys = schema.entity ? schema.entity.attributes.map((a) => a.key) : Object.keys(entity.state)
  return orderedKeys
    .filter((k) => k !== 'stage' && k in entity.state)
    .map((k) => [k, entity.state[k]] as [string, string | null])
    .slice(0, 3)
}

function EntityCard({ entity, schema, allStages, onMove, onOpenEntity }: {
  entity: TaskEntity
  schema: TaskStateSchema
  allStages: string[]
  onMove: (entityId: string, stage: string) => void
  onOpenEntity: (entityId: string) => void
}) {
  const fields = nonStageFieldEntries(entity, schema)
  const currentStage = entity.state.stage ?? ''
  return (
    <div onClick={() => onOpenEntity(entity.id)} style={cardStyle}>
      <div style={{ fontWeight: 600, fontSize: 13 }}>{entity.display_name}</div>
      {fields.map(([k, v]) => (
        <div key={k} style={{ fontSize: 11, color: '#666' }}>{k}: {v ?? '—'}</div>
      ))}
      {/* stopPropagation so interacting with the select doesn't also bubble
          into the card's own onClick above — only the card body (not the
          select) opens the drawer, per the plan. */}
      <div onClick={(ev) => ev.stopPropagation()} style={{ marginTop: 6 }}>
        <select
          value={currentStage}
          onChange={(ev) => {
            const next = ev.target.value
            if (next && next !== currentStage) onMove(entity.id, next)
          }}
          style={{ fontSize: 11, width: '100%' }}
        >
          <option value="" disabled>move to ▾</option>
          {allStages.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>
    </div>
  )
}

const cardStyle: CSSProperties = {
  padding: '6px 8px', border: '1px solid #eee', borderRadius: 6, background: '#fff',
  cursor: 'pointer', fontSize: 13,
}
