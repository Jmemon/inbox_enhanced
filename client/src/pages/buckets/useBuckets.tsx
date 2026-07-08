import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  type Bucket,
  getTasks, patchTask, deleteTask,
} from '../../lib/api'


export function useBuckets() {
  const [buckets, setBuckets] = useState<Bucket[]>([])
  const [loading, setLoading] = useState(true)

  // Buckets are tasks(kind='bucket') (Phase 4) — GET /api/tasks?kind=bucket
  // is the task-backed replacement for the old dedicated bucket list, and
  // its rows carry the same criteria/is_default fields the old shape did.
  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const { tasks } = await getTasks({ kind: 'bucket' })
      setBuckets(tasks.map(t => ({
        id: t.id, name: t.name, criteria: t.criteria ?? '', is_default: t.is_default ?? false,
      })))
    } finally { setLoading(false) }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const byId = useMemo(() => Object.fromEntries(buckets.map(b => [b.id, b])), [buckets])
  const customBuckets = useMemo(() => buckets.filter(b => !b.is_default), [buckets])

  const rename = useCallback(async (id: string, name: string) => {
    await patchTask(id, { name }); await refresh()
  }, [refresh])

  const softDelete = useCallback(async (id: string) => {
    await deleteTask(id); await refresh()
  }, [refresh])

  return { buckets, byId, customBuckets, loading, refresh, rename, softDelete }
}
