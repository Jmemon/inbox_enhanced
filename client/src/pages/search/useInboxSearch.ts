import { useEffect, useRef, useState } from 'react'
import { searchInbox, type InboxThread } from '../../lib/api'

// Debounced server search against /api/search with a monotonic request token:
// stale responses (an earlier query resolving after a later one) are dropped,
// and a late response can't resurrect search mode after the user cleared it.
export function useInboxSearch() {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<InboxThread[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const searchSeq = useRef(0)

  useEffect(() => {
    const q = query.trim()
    if (!q) {
      searchSeq.current++
      setResults(null); setError(null)
      return
    }
    const seq = ++searchSeq.current
    const t = setTimeout(async () => {
      try {
        const r = await searchInbox(q)
        if (seq !== searchSeq.current) return
        setResults(r.threads); setError(null)
      } catch (e: any) {
        if (seq !== searchSeq.current) return
        // Engage search mode so the error is visible even on a first search.
        setResults([]); setError(String(e?.message ?? e))
      }
    }, 300)
    return () => clearTimeout(t)
  }, [query])

  return { query, setQuery, results, error }
}
