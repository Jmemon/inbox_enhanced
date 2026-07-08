import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  type Bucket, type BucketExampleIn,
  getTasks, createTask, patchTask, deleteTask,
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

  // NOTE: this survives only for the shim window — Task 5 moves bucket
  // creation into the task wizard. If nothing calls `create` once Task 5
  // lands, delete it then.
  const create = useCallback(async (body: {
    name: string; description: string;
    confirmed_positives: BucketExampleIn[]; confirmed_negatives: BucketExampleIn[];
  }): Promise<Bucket> => {
    const created = await createTask({
      ...body, goal: body.description, kind: 'bucket', state_schema: null, keyword_probes: [],
    })
    await refresh()
    return { id: created.id, name: created.name, criteria: created.criteria, is_default: false }
  }, [refresh])

  const rename = useCallback(async (id: string, name: string) => {
    await patchTask(id, { name }); await refresh()
  }, [refresh])

  const softDelete = useCallback(async (id: string) => {
    await deleteTask(id); await refresh()
  }, [refresh])

  return { buckets, byId, customBuckets, loading, refresh, create, rename, softDelete }
}
