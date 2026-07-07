import { useState } from 'react'
import type { Bucket, InboxThread } from '../../lib/api'
import { SearchBar } from '../search/SearchBar'
import { useInboxSearch } from '../search/useInboxSearch'

// Copied from InboxList's private (unexported) `abbreviate` helper rather
// than imported: reusing InboxList itself for these rows would drag in its
// bucket-pill grid layout and column widths, which this panel's thin
// subject/sender/date rows (+ an actions column InboxList has no room for)
// don't want — see Task 8 brief ("reuse InboxList with an extra actions
// column is invasive — build a thin local row component reusing its
// abbreviate helper by export or copy"). Keep in sync with
// pages/inbox/InboxList.tsx's version if it changes.
function abbreviate(addr: string | null | undefined): string {
  if (!addr) return '?'
  // "Alice Smith <alice@x.com>" → "Alice Smith"; bare email → username before @
  const m = addr.match(/^([^<]+)<.*?>$/)
  const name = (m ? m[1] : addr).trim()
  if (name.includes('@')) return name.split('@')[0]
  return name
}

// recent_message.internal_date is gmail_internal_date in epoch MILLISECONDS
// (see server/app/gmail/parser.py's `int(raw.get("internalDate"...`) — no
// /1000 needed, unlike HudPage's agoLabel which takes epoch seconds.
function dateLabel(internalDateMs: number | undefined): string {
  if (!internalDateMs) return ''
  return new Date(internalDateMs).toLocaleDateString()
}

function ThreadSummary({ thread, bucket }: { thread: InboxThread; bucket?: Bucket }) {
  return (
    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
      <span style={{ fontWeight: 500 }}>{thread.subject || '(no subject)'}</span>
      <span style={{ color: '#888' }}> · {abbreviate(thread.recent_message?.from ?? null)}</span>
      <span style={{ color: '#aaa' }}> · {dateLabel(thread.recent_message?.internal_date)}</span>
      {bucket && <span style={{ color: '#888' }}> · {bucket.name}</span>}
    </span>
  )
}

// One attached thread: subject / sender / date (+ bucket pill, kept simple —
// just its name, no colored chip), a per-row "teach the task" checkbox
// (default checked — matches the API's add_example default), and a detach
// button. Checkbox state is local to the row, so toggling one thread's
// example-teaching choice doesn't affect any other row.
function AttachedRow({ thread, bucket, onDetach }: {
  thread: InboxThread
  bucket: Bucket | undefined
  onDetach: (threadId: string, addExample: boolean) => void
}) {
  const [addExample, setAddExample] = useState(true)
  return (
    <li style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
      padding: '6px 8px', border: '1px solid #eee', borderRadius: 6, fontSize: 13,
    }}>
      <ThreadSummary thread={thread} bucket={bucket} />
      <label
        style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, color: '#666', flexShrink: 0 }}
        title="adds this as a near-miss example so the task stops matching threads like it"
      >
        <input type="checkbox" checked={addExample} onChange={(e) => setAddExample(e.target.checked)} />
        teach the task
      </label>
      <button onClick={() => onDetach(thread.id, addExample)} style={{ fontSize: 12, flexShrink: 0 }}>
        detach
      </button>
    </li>
  )
}

// One search result: same row shape, but "attach" instead of "detach" and
// the opposite checkbox polarity — checked means "use as a positive
// example" instead of a near-miss. Same default-checked UX, different
// tooltip copy per the brief.
function SearchResultRow({ thread, onAttach }: {
  thread: InboxThread
  onAttach: (threadId: string, addExample: boolean) => void
}) {
  const [addExample, setAddExample] = useState(true)
  return (
    <li style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
      padding: '6px 8px', border: '1px solid #eee', borderRadius: 6, fontSize: 13,
    }}>
      <ThreadSummary thread={thread} />
      <label
        style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, color: '#666', flexShrink: 0 }}
        title="adds this as a positive example"
      >
        <input type="checkbox" checked={addExample} onChange={(e) => setAddExample(e.target.checked)} />
        teach the task
      </label>
      <button onClick={() => onAttach(thread.id, addExample)} style={{ fontSize: 12, flexShrink: 0 }}>
        attach
      </button>
    </li>
  )
}

// `taskId` isn't consumed here — /api/search has no task-scoping param (see
// server/app/api/search.py), so the "Add emails" search below searches the
// whole inbox, same as HudPage's. It stays in the prop contract for a future
// task-scoped search without another signature change.
export function ThreadsPanel({ taskId: _taskId, threads, bucketsById, onDetach, onAttach }: {
  taskId: string
  threads: InboxThread[]
  bucketsById: Record<string, Bucket>
  onDetach: (threadId: string, addExample: boolean) => void
  onAttach: (threadId: string, addExample: boolean) => void
}) {
  const search = useInboxSearch()

  const attachedIds = new Set(threads.map((t) => t.id))
  // Already-attached threads shouldn't also show up as attachable search
  // results — the brief calls this out explicitly.
  const searchResults = (search.results ?? []).filter((t) => !attachedIds.has(t.id))

  return (
    <div style={{ border: '1px solid #eee', borderRadius: 8, padding: 16, display: 'grid', gap: 16 }}>
      <div>
        <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>
          threads — {threads.length} attached
        </div>
        {threads.length === 0 && <div style={{ color: '#888', fontSize: 13 }}>no threads attached yet</div>}
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 6 }}>
          {threads.map((t) => (
            <AttachedRow
              key={t.id}
              thread={t}
              bucket={t.bucket_id ? bucketsById[t.bucket_id] : undefined}
              onDetach={onDetach}
            />
          ))}
        </ul>
      </div>

      <div>
        <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>Add emails</div>
        <SearchBar query={search.query} setQuery={search.setQuery} results={search.results} />
        {search.error && (
          <div style={{ color: '#8a1c25', fontSize: 12, marginTop: 8 }}>search error: {search.error}</div>
        )}
        {search.results !== null && (
          <ul style={{ listStyle: 'none', margin: '8px 0 0', padding: 0, display: 'grid', gap: 6 }}>
            {searchResults.length === 0 && <div style={{ color: '#888', fontSize: 13 }}>no results</div>}
            {searchResults.map((t) => (
              <SearchResultRow key={t.id} thread={t} onAttach={onAttach} />
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
