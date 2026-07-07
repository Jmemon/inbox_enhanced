import { useEffect, useRef } from 'react'
import type { CSSProperties } from 'react'
import type { TaskStateSchema } from '../../lib/api'

// The five attribute types the server's EPS schema accepts (server/app/
// task_engine/schema.py's ATTR_TYPES) — this is a small, fixed vocabulary
// enforced server-side, so it's just mirrored here rather than fetched.
const ATTR_TYPES = ['string', 'number', 'datetime', 'boolean', 'enum'] as const

type Entity = NonNullable<TaskStateSchema['entity']>
type Attribute = Entity['attributes'][number]

const EMPTY_ENTITY: Entity = { noun: '', identity_hint: '', attributes: [] }

// Purely controlled editor over a task's `state_schema`: pipeline stages +
// terminal stages as inline-editable chip lists, an optional entity
// (noun/identity_hint + typed attribute rows), and a "singleton task" toggle
// that nulls `entity` out while preserving the last entity edits locally so
// un-toggling restores them instead of resetting to blank. No validation
// beyond non-empty trims on the pipeline stage fields below — the server's
// state_schema validator (task_engine/schema.py) is the source of truth and
// surfaces as a 422 on create, which the wizard renders inline.
export function SchemaEditor({ value, onChange }: {
  value: TaskStateSchema
  onChange: (s: TaskStateSchema) => void
}) {
  const isSingleton = value.entity === null

  // Remembers the last non-null entity so toggling "singleton task"
  // off -> on -> off round-trips the user's edits instead of resetting to a
  // blank entity each time. Synced via effect (one render behind, never
  // written mid-render) — same rationale as TasksProvider's byIdRef.
  const lastEntityRef = useRef<Entity>(value.entity ?? EMPTY_ENTITY)
  useEffect(() => {
    if (value.entity) lastEntityRef.current = value.entity
  }, [value.entity])

  function toggleSingleton(checked: boolean) {
    onChange({ ...value, entity: checked ? null : lastEntityRef.current })
  }

  function updateEntity(patch: Partial<Pick<Entity, 'noun' | 'identity_hint'>>) {
    if (!value.entity) return
    onChange({ ...value, entity: { ...value.entity, ...patch } })
  }

  function updateAttribute(index: number, patch: Partial<Attribute>) {
    if (!value.entity) return
    const attributes = value.entity.attributes.map((a, i) => (i === index ? { ...a, ...patch } : a))
    onChange({ ...value, entity: { ...value.entity, attributes } })
  }

  function addAttribute() {
    if (!value.entity) return
    const attributes = [...value.entity.attributes, { key: '', type: 'string' as const, values: null }]
    onChange({ ...value, entity: { ...value.entity, attributes } })
  }

  function removeAttribute(index: number) {
    if (!value.entity) return
    const attributes = value.entity.attributes.filter((_, i) => i !== index)
    onChange({ ...value, entity: { ...value.entity, attributes } })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <ChipList
        label="pipeline stages"
        items={value.pipeline.stages}
        onChange={stages => onChange({ ...value, pipeline: { ...value.pipeline, stages } })}
      />
      <ChipList
        label="terminal stages"
        items={value.pipeline.terminal}
        onChange={terminal => onChange({ ...value, pipeline: { ...value.pipeline, terminal } })}
      />

      <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
        <input type="checkbox" checked={isSingleton} onChange={e => toggleSingleton(e.target.checked)} />
        singleton task (tracks one implicit thing — no named entities)
      </label>

      {!isSingleton && value.entity && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div style={{ display: 'flex', gap: 12 }}>
            <label style={fieldLabelStyle}>
              <span>entity noun</span>
              <input
                style={inputStyle}
                value={value.entity.noun}
                placeholder="e.g. company"
                onChange={e => updateEntity({ noun: e.target.value })}
              />
            </label>
            <label style={fieldLabelStyle}>
              <span>identity hint</span>
              <input
                style={inputStyle}
                value={value.entity.identity_hint}
                placeholder="how to tell two of these apart"
                onChange={e => updateEntity({ identity_hint: e.target.value })}
              />
            </label>
          </div>

          <div>
            <div style={{ fontSize: 12, color: '#666', marginBottom: 6 }}>attributes</div>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <tbody>
                {value.entity.attributes.map((attr, i) => (
                  <tr key={i}>
                    <td style={tdStyle}>
                      <input
                        style={inputStyle} value={attr.key} placeholder="key"
                        onChange={e => updateAttribute(i, { key: e.target.value })}
                      />
                    </td>
                    <td style={tdStyle}>
                      <select
                        style={inputStyle} value={attr.type}
                        onChange={e => updateAttribute(i, {
                          type: e.target.value,
                          // Only enum attributes carry `values` (server's
                          // AttributeSpec rejects values on any other type,
                          // and requires them on enum) — clear/seed on switch.
                          values: e.target.value === 'enum' ? (attr.values ?? []) : null,
                        })}
                      >
                        {ATTR_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                      </select>
                    </td>
                    <td style={tdStyle}>
                      {attr.type === 'enum' && (
                        <input
                          style={inputStyle}
                          placeholder="comma, separated, values"
                          value={(attr.values ?? []).join(', ')}
                          onChange={e => updateAttribute(i, {
                            values: e.target.value.split(',').map(s => s.trim()).filter(Boolean),
                          })}
                        />
                      )}
                    </td>
                    <td style={tdStyle}>
                      <button onClick={() => removeAttribute(i)} aria-label={`remove attribute ${attr.key}`}>×</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <button style={{ marginTop: 8 }} onClick={addAttribute}>add attribute</button>
          </div>
        </div>
      )}
    </div>
  )
}

// Editable chip list backing the pipeline's `stages`/`terminal` arrays: each
// chip is itself a text input (rename in place) plus a remove button; "add"
// appends a blank chip that's immediately editable — no separate input
// buffer needed since the array element itself IS the draft text.
function ChipList({ label, items, onChange }: {
  label: string; items: string[]; onChange: (items: string[]) => void
}) {
  function update(i: number, v: string) {
    onChange(items.map((s, idx) => (idx === i ? v : s)))
  }
  function remove(i: number) {
    onChange(items.filter((_, idx) => idx !== i))
  }
  return (
    <div>
      <div style={{ fontSize: 12, color: '#666', marginBottom: 6 }}>{label}</div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
        {items.map((item, i) => (
          <div key={i} style={chipStyle}>
            <input style={chipInputStyle} value={item} onChange={e => update(i, e.target.value)} />
            <button style={chipRemoveStyle} onClick={() => remove(i)} aria-label={`remove ${label} entry`}>×</button>
          </div>
        ))}
        <button onClick={() => onChange([...items, ''])}>+ add</button>
      </div>
    </div>
  )
}

const fieldLabelStyle: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 6, fontSize: 14, flex: 1 }

const inputStyle: CSSProperties = {
  width: '100%', boxSizing: 'border-box', padding: '6px 8px', fontSize: 14,
  borderRadius: 4, border: '1px solid #ccc',
}

const tdStyle: CSSProperties = { padding: '4px 6px', verticalAlign: 'middle' }

const chipStyle: CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 4, background: '#f2f4f7',
  borderRadius: 14, padding: '2px 4px 2px 10px',
}

const chipInputStyle: CSSProperties = {
  border: 'none', background: 'transparent', fontSize: 13, padding: '2px 0', width: 100,
}

const chipRemoveStyle: CSSProperties = {
  border: 'none', background: 'transparent', cursor: 'pointer', color: '#888', fontSize: 14, lineHeight: 1,
}
