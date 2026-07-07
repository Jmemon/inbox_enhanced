import { useState } from 'react'
import type { Bucket, InboxThread } from '../../lib/api'

// PLACEHOLDER — Task 8 (plans/2026-07-06-phase2b-task-ui.md) replaces this
// file's body with the real threads panel: InboxList-style attached rows
// (with a "teach the task" add_example checkbox) plus a SearchBar +
// useInboxSearch "Add emails" section. The prop shapes below are the Task
// 6/8 contract — TaskDetail already wires real callbacks (attach/detach →
// api call + refetch) against them, so keep the shapes stable across the
// rewrite. `taskId` isn't needed by this flat stub (no search scoping yet)
// but is part of the contract Task 8's search-to-attach flow will consume.
export function ThreadsPanel({ taskId: _taskId, threads, bucketsById, onDetach, onAttach }: {
  taskId: string
  threads: InboxThread[]
  bucketsById: Record<string, Bucket>
  onDetach: (threadId: string, addExample: boolean) => void
  onAttach: (threadId: string, addExample: boolean) => void
}) {
  const [attachId, setAttachId] = useState('')

  return (
    <div style={{ border: '1px solid #eee', borderRadius: 8, padding: 16 }}>
      <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>
        threads — {threads.length} attached
      </div>
      {threads.length === 0 && <div style={{ color: '#888', fontSize: 13 }}>no threads attached yet</div>}
      <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 6 }}>
        {threads.map((t) => {
          const bucket = t.bucket_id ? bucketsById[t.bucket_id] : undefined
          return (
            <li key={t.id} style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
              padding: '6px 8px', border: '1px solid #eee', borderRadius: 6, fontSize: 13,
            }}>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {t.subject || '(no subject)'}{bucket && <span style={{ color: '#888' }}> · {bucket.name}</span>}
              </span>
              {/* Default add_example=true — matches the API's default and the
                  plan's "teach the task" checkbox semantics (Task 8 adds the
                  actual toggle; this stub always teaches on detach). */}
              <button onClick={() => onDetach(t.id, true)} style={{ fontSize: 12 }}>detach</button>
            </li>
          )
        })}
      </ul>
      <div style={{ marginTop: 12, display: 'flex', gap: 6, alignItems: 'center' }}>
        <input
          value={attachId}
          onChange={(ev) => setAttachId(ev.target.value)}
          placeholder="thread id to attach"
          style={{ fontSize: 12, flex: 1 }}
        />
        <button
          onClick={() => {
            const id = attachId.trim()
            if (!id) return
            onAttach(id, true)
            setAttachId('')
          }}
          style={{ fontSize: 12 }}
        >
          attach
        </button>
      </div>
    </div>
  )
}
