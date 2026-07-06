import type { InboxThread } from '../../lib/api'

export function SearchBar({ query, setQuery, results }: {
  query: string
  setQuery: (q: string) => void
  results: InboxThread[] | null
}) {
  return (
    <div style={{ padding: '8px 24px', borderBottom: '1px solid #eee' }}>
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="search your inbox…"
        style={{ width: 360, maxWidth: '100%', padding: '6px 10px', fontSize: 14 }}
      />
      {results !== null && (
        <span style={{ marginLeft: 12, fontSize: 12, color: '#888' }}>
          {results.length} result{results.length === 1 ? '' : 's'}
          <button onClick={() => setQuery('')} style={{ marginLeft: 8, fontSize: 12 }}>clear</button>
        </span>
      )}
    </div>
  )
}
