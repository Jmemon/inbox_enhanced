import { useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import type { Bucket } from '../../lib/api'

const DELETE_COPY =
  "Deleting this bucket means no new threads will be classified into it. " +
  "Threads currently classified into this bucket keep that classification but " +
  "show as unclassified going forward, and the bucket won't appear in your filter dropdown."


export function ViewBucketsModal({
  buckets, onClose, onRename, onDelete,
}: {
  buckets: Bucket[]
  onClose: () => void
  onRename: (id: string, name: string) => Promise<void>
  onDelete: (id: string) => Promise<void>
}) {
  const [editing, setEditing] = useState<{ id: string; draft: string } | null>(null)
  const [confirming, setConfirming] = useState<string | null>(null)

  // `confirming` is a single id — only one bucket can be mid-delete-confirm
  // at a time — so the confirm block itself renders once, directly under the
  // header, rather than inline inside whichever <li> happens to own that id.
  // Previously it rendered per-item below that bucket's (potentially long)
  // criteria block, which could put the confirm/cancel buttons below the
  // fold for a bucket further down the list; here it's always visible the
  // moment "delete" is clicked (design.md §2.5).
  const confirmingBucket = confirming ? buckets.find(b => b.id === confirming) ?? null : null

  return (
    <Backdrop onClose={onClose}>
      <div style={modalStyle}>
        <h3 style={{ margin: 0 }}>buckets</h3>
        {confirmingBucket && (
          <div style={{ background: '#fff8e0', padding: 8, marginTop: 12, fontSize: 13 }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>Delete "{confirmingBucket.name}"?</div>
            {DELETE_COPY}
            <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
              <button onClick={async () => { await onDelete(confirmingBucket.id); setConfirming(null) }}>
                delete
              </button>
              <button onClick={() => setConfirming(null)}>cancel</button>
            </div>
          </div>
        )}
        <ul style={{ listStyle: 'none', padding: 0, margin: '12px 0 0 0' }}>
          {buckets.map(b => (
            <li key={b.id} style={{ padding: '12px 0', borderBottom: '1px solid #eee' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                {editing?.id === b.id ? (
                  <input value={editing.draft} autoFocus
                         onChange={e => setEditing({ id: b.id, draft: e.target.value })}
                         onBlur={async () => {
                           if (editing.draft && editing.draft !== b.name)
                             await onRename(b.id, editing.draft)
                           setEditing(null)
                         }} />
                ) : <strong>{b.name}{b.is_default ? ' (default)' : ''}</strong>}
                {!b.is_default && (
                  <div style={{ display: 'flex', gap: 8 }}>
                    <button onClick={() => setEditing({ id: b.id, draft: b.name })}>rename</button>
                    <button onClick={() => setConfirming(b.id)}>delete</button>
                  </div>
                )}
              </div>
              <pre style={{ fontSize: 12, color: '#666', whiteSpace: 'pre-wrap', marginTop: 8 }}>
                {b.criteria}
              </pre>
            </li>
          ))}
        </ul>
        <div style={{ marginTop: 12 }}><button onClick={onClose}>close</button></div>
      </div>
    </Backdrop>
  )
}


const modalStyle: CSSProperties = {
  background: '#fff', padding: 24, borderRadius: 8, maxWidth: 640, width: '90%',
  maxHeight: '80vh', overflowY: 'auto',
}

function Backdrop({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
    }}>
      <div onClick={e => e.stopPropagation()}>{children}</div>
    </div>
  )
}
