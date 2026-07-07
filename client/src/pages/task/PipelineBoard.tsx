import type { TaskEntity, TaskStateSchema } from '../../lib/api'

// PLACEHOLDER — Task 7 (plans/2026-07-06-phase2b-task-ui.md) replaces this
// file's body with the real board: one column per pipeline stage (terminal
// stages dimmed), a singleton-schema (`entity: null`) one-row rendering, and
// an EntityDrawer opened via onOpenEntity. The prop shapes below are the
// Task 6/7 contract — TaskDetail already wires real callbacks against them,
// so keep the shapes stable across the rewrite. This stub renders a flat
// list with a per-card stage <select> so the wiring is exercised end to end
// before Task 7 lands.
export function PipelineBoard({ schema, entities, onMove, onOpenEntity }: {
  schema: TaskStateSchema | null
  entities: TaskEntity[]
  onMove: (entityId: string, stage: string) => void
  onOpenEntity: (entityId: string) => void
}) {
  if (!schema) {
    return <div style={{ padding: 16, color: '#888', fontSize: 13 }}>schema pending…</div>
  }
  const stages = [...schema.pipeline.stages, ...schema.pipeline.terminal]
  return (
    <div style={{ border: '1px solid #eee', borderRadius: 8, padding: 16 }}>
      <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>
        board — {entities.length} {entities.length === 1 ? 'entity' : 'entities'}
      </div>
      {entities.length === 0 && <div style={{ color: '#888', fontSize: 13 }}>no entities yet</div>}
      <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 6 }}>
        {entities.map((e) => (
          <li key={e.id} style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
            padding: '6px 8px', border: '1px solid #eee', borderRadius: 6, fontSize: 13,
          }}>
            <button
              onClick={() => onOpenEntity(e.id)}
              style={{ border: 'none', background: 'none', padding: 0, textAlign: 'left', cursor: 'pointer', font: 'inherit' }}
            >
              <span style={{ fontWeight: 600 }}>{e.display_name}</span>
            </button>
            {/* Stage move IS v1's correction gesture (no drag library) — Task
                7's real board keeps this same <select>-per-card mechanism,
                just grouped into columns instead of a flat list. */}
            <select
              value={e.state.stage ?? ''}
              onChange={(ev) => {
                const next = ev.target.value
                if (next && next !== (e.state.stage ?? '')) onMove(e.id, next)
              }}
              style={{ fontSize: 12 }}
            >
              <option value="" disabled>(no stage)</option>
              {stages.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </li>
        ))}
      </ul>
    </div>
  )
}
