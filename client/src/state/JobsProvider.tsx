import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  confirmJob as apiConfirmJob, createJob, dismissJob as apiDismissJob, getJobs,
  type ConfirmJobBody, type Job, type TaskDetail,
} from '../lib/api'
import { subscribeSse } from '../lib/sse'
import { useInboxStore } from './InboxProvider'

// Mounted once in AppShell (inside TasksProvider, for the life of the authed
// session) — same rationale as TasksProvider/InboxProvider: the jobs list
// and its SSE subscription need to survive route navigation instead of
// remounting from scratch (and re-subscribing to SSE) every time the header
// chip that reads it would otherwise unmount/remount.

const POLL_INTERVAL_MS = 15_000

// Spec §1.2 (specs/005_jobs_surface/design.md): a job is terminal once its
// stage is 'done'/'failed' OR it's been dismissed — the literal string
// 'dismissed' never appears in `stage` itself. Exported so JobsChip's own
// "is anything still running" spinner check uses the exact same definition
// the poll-gating effect below does, rather than two definitions drifting.
export function isJobTerminal(job: Job): boolean {
  return job.dismissed_at !== null || job.stage === 'done' || job.stage === 'failed'
}

type JobsStore = {
  jobs: Job[]
  refresh: () => Promise<void>
  startCreation: (goal: string, taskKind: 'tracker' | 'bucket') => Promise<Job>
  confirmJob: (id: string, body: ConfirmJobBody) => Promise<{ task: TaskDetail; job: Job }>
  dismissJob: (id: string) => Promise<void>
}

const JobsStoreContext = createContext<JobsStore | null>(null)

export function JobsProvider({ children }: { children: ReactNode }) {
  // Bucket-creation completion has nowhere else to land: bucket mode's old
  // onCreated callback used to fire `buckets.refresh()` once the wizard saw
  // its own backfill finish, but the wizard now closes at confirm-time —
  // long before the job reaches 'done'. useBuckets (pages/buckets/useBuckets)
  // never subscribes to SSE at all, and TasksProvider's task_updated handler
  // only ever refetches the TRACKER list (getTasks() with no kind filter),
  // so nothing else in the app will show a freshly-backfilled bucket without
  // a manual reload. AppShell nests JobsProvider inside InboxProvider
  // specifically so a provider in this position can reach another store
  // (see AppShell.tsx's hook-order note) — that's the seam used here rather
  // than threading a callback prop down from AppShell.
  const { buckets: { refresh: refreshBuckets } } = useInboxStore()
  const [jobs, setJobs] = useState<Job[]>([])

  // Previous fetch's jobs, keyed for the done-transition diff below. A ref
  // (not state) because it's write-only from refresh's own perspective — it
  // never drives a render itself, only informs the next refresh's diff.
  const prevJobsRef = useRef<Job[]>([])

  const refresh = useCallback(async () => {
    try {
      // active=true (the default) — the panel's normal, always-works poll
      // path (spec §1.4); dismissed/long-stale jobs are excluded server-side.
      const fetched = await getJobs()
      // Fire buckets.refresh() exactly on a bucket-kind creation job's
      // transition INTO 'done' (compared against the previous fetch by id),
      // not on every poll tick while such a job merely sits at 'done' in the
      // active list — list_jobs keeps terminal jobs active for 7 days, so
      // without this diff every 15s tick would re-refresh the bucket list
      // for as long as the done job stays visible.
      const prevById = new Map(prevJobsRef.current.map(j => [j.id, j]))
      let shouldRefreshBuckets = false
      for (const job of fetched) {
        if (job.kind !== 'creation' || job.task_kind !== 'bucket' || job.stage !== 'done') continue
        const prev = prevById.get(job.id)
        if (!prev || prev.stage !== 'done') shouldRefreshBuckets = true
      }
      if (shouldRefreshBuckets) refreshBuckets().catch(e => console.error('[JobsProvider] bucket refresh failed', e))
      prevJobsRef.current = fetched
      setJobs(fetched)
    } catch (e) {
      console.error('[JobsProvider] refresh failed', e)
    }
  }, [refreshBuckets])

  // Initial catch-up fetch. Not redundant with the `_open` handler below —
  // see TasksProvider's identical comment: `_open` only fires for handlers
  // already subscribed before the EventSource connects, and by the time this
  // provider mounts (inside InboxProvider/TasksProvider) that first `_open`
  // is usually long consumed.
  useEffect(() => { void refresh() }, [refresh])

  useEffect(() => {
    return subscribeSse((e) => {
      if (e.event === '_open') {
        void refresh()
        return
      }
      // job_updated is a pure nudge (job_id only, never a row, per this
      // app's SSE convention) — refetch the whole list rather than trying
      // to patch a single job in place.
      if (e.event === 'job_updated') void refresh()
    })
  }, [refresh])

  // 15s poll — belt to SSE's suspenders (spec §2.1): a dropped connection
  // only delays updates until the next tick instead of stranding the panel.
  // Active ONLY while some job is non-terminal, and must start/stop as the
  // jobs list itself changes (not just on mount/unmount) — the load-bearing
  // property here is that this effect's own cleanup (clearInterval) runs
  // every time `hasActive` flips, so the moment the last job finishes there
  // is no interval left ticking in the background.
  const hasActive = jobs.some(j => !isJobTerminal(j))
  useEffect(() => {
    if (!hasActive) return
    const id = setInterval(() => { void refresh() }, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [hasActive, refresh])

  const startCreation = useCallback(async (goal: string, taskKind: 'tracker' | 'bucket'): Promise<Job> => {
    const job = await createJob(goal, taskKind)
    await refresh()
    return job
  }, [refresh])

  const confirmJobFn = useCallback(async (id: string, body: ConfirmJobBody) => {
    const result = await apiConfirmJob(id, body)
    await refresh()
    return result
  }, [refresh])

  const dismissJobFn = useCallback(async (id: string): Promise<void> => {
    await apiDismissJob(id)
    await refresh()
  }, [refresh])

  const value = useMemo(
    () => ({ jobs, refresh, startCreation, confirmJob: confirmJobFn, dismissJob: dismissJobFn }),
    [jobs, refresh, startCreation, confirmJobFn, dismissJobFn],
  )

  return <JobsStoreContext.Provider value={value}>{children}</JobsStoreContext.Provider>
}

export function useJobsStore(): JobsStore {
  const ctx = useContext(JobsStoreContext)
  if (!ctx) {
    throw new Error('useJobsStore must be used within a <JobsProvider> (mounted once in AppShell)')
  }
  return ctx
}
