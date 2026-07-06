import type { Bucket, InboxThread } from '../../lib/api'

function abbreviate(addr: string | null | undefined): string {
  if (!addr) return '?'
  // "Alice Smith <alice@x.com>" → "Alice Smith"; bare email → username before @
  const m = addr.match(/^([^<]+)<.*?>$/)
  const name = (m ? m[1] : addr).trim()
  if (name.includes('@')) return name.split('@')[0]
  return name
}

// Small chip showing which bucket a thread fell into. Renders an em dash when
// the thread has no bucket_id (e.g., not yet classified) so the column stays
// visually aligned across rows.
function BucketPill({ bucket }: { bucket: Bucket | undefined }) {
  if (!bucket) {
    return <span style={{ color: '#bbb', fontSize: 12 }}>—</span>
  }
  return (
    <span
      title={bucket.name}
      style={{
        display: 'inline-block',
        maxWidth: '100%',
        padding: '2px 8px',
        borderRadius: 999,
        background: bucket.is_default ? '#eef2f7' : '#e7f1ea',
        color: bucket.is_default ? '#4b5563' : '#2f6b46',
        fontSize: 12,
        fontWeight: 500,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
      }}
    >
      {bucket.name}
    </span>
  )
}

export function InboxList({
  threads,
  bucketsById,
  emptyLabel = 'syncing your inbox…',
}: {
  threads: InboxThread[]
  bucketsById: Record<string, Bucket>
  // Overridable copy for the empty state — the default reads correctly for
  // an inbox that hasn't synced yet, but is misleading for an empty search
  // result (search call sites pass "no results" instead).
  emptyLabel?: string
}) {
  if (threads.length === 0) {
    return <div style={{ padding: 24, color: '#666' }}>{emptyLabel}</div>
  }
  return (
    <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
      {threads.map((t) => {
        const from = abbreviate(t.recent_message?.from ?? null)
        const preview = t.recent_message?.body_preview ?? ''
        const bucket = t.bucket_id ? bucketsById[t.bucket_id] : undefined
        return (
          <li
            key={t.id}
            style={{
              display: 'grid',
              gridTemplateColumns: '140px 160px 1fr 2fr',
              gap: 16,
              padding: '10px 16px',
              borderBottom: '1px solid #eee',
              fontSize: 14,
              alignItems: 'baseline',
            }}
          >
            <div style={{ overflow: 'hidden' }}><BucketPill bucket={bucket} /></div>
            <div style={{ color: '#222', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{from}</div>
            <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.subject || '(no subject)'}</div>
            <div style={{ color: '#666', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{preview}</div>
          </li>
        )
      })}
    </ul>
  )
}
